"""The search index: one SQLite database in memory, saved and restored whole.

Tables: `talks` (one row a talk with its names), `passages` (see passages.py), the FTS5 tables `texts` (a passage's
Gujarati, Gujlish, English and summary text, plus its words' spelling keys) and `titles` (a talk's names and their
keys), `vocab` and `near` (every spelling key in use and its one-letter deletions, for typos) and `vectors` (one
embedding per distinct text, float16). Embeddings are mirrored in a `VectorTable` in RAM for exact cosine search.

`put_talk` compares passages by digest, so a talk that changed in one place rewrites one row and needs one new
embedding. Every public method holds one lock; the service calls them from request threads and one worker.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import weakref
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .passages import Passage, line_at, meaning_text
from .text import GUJARATI, NEAR_MIN, TOKEN_CHARS, WORD, edit_variants, fold, similar, sound_key, words

SCHEMA_VERSION = "2"
TOKENIZER = f"porter unicode61 remove_diacritics 2 tokenchars '{TOKEN_CHARS}'"
LIST_LIMIT = 300  # rows taken from each ranked list before fusing
RRF_K = 60  # reciprocal-rank fusion: a row's score is the sum of weight / (RRF_K + rank) over the lists it is in
WEIGHTS = {"words": 1.0, "spelling": 0.6, "meaning": 0.8, "every": 1.0}
NAME_WEIGHTS = {"words": 1.5, "spelling": 0.9, "every": 1.5}
MEANING_K = 40
MEANING_FLOOR = 0.45  # cosine similarity below which a passage is not "about" the query
MEANING_BAND = 0.15  # and never further than this below the best match
SNIPPET_WORDS = 30
SNIPPET_LEAD = 8  # words shown before the first match

SCHEMA = f"""
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE talks(
    id INTEGER PRIMARY KEY, talk TEXT NOT NULL UNIQUE, stamp TEXT NOT NULL DEFAULT '',
    given TEXT NOT NULL DEFAULT '', auto TEXT NOT NULL DEFAULT '', file TEXT NOT NULL DEFAULT '',
    keys TEXT NOT NULL DEFAULT '');
CREATE TABLE passages(
    id INTEGER PRIMARY KEY, talk INTEGER NOT NULL, kind TEXT NOT NULL, n INTEGER NOT NULL, start REAL, stop REAL,
    seg INTEGER, label TEXT NOT NULL, gu TEXT NOT NULL, gl TEXT NOT NULL, en TEXT NOT NULL, sm TEXT NOT NULL,
    lines TEXT NOT NULL, keys TEXT NOT NULL, digest TEXT NOT NULL, vec TEXT, UNIQUE(talk, kind, n));
CREATE INDEX passages_vec ON passages(vec);
CREATE VIRTUAL TABLE texts USING fts5(
    gu, gl, en, sm, keys, content='passages', content_rowid='id', tokenize="{TOKENIZER}", prefix='2 3');
CREATE VIRTUAL TABLE titles USING fts5(given, auto, file, keys, tokenize="{TOKENIZER}", prefix='2 3');
CREATE TABLE vocab(key TEXT PRIMARY KEY, uses INTEGER NOT NULL) WITHOUT ROWID;
CREATE TABLE near(variant TEXT NOT NULL, key TEXT NOT NULL, PRIMARY KEY(variant, key)) WITHOUT ROWID;
CREATE TABLE vectors(key TEXT NOT NULL UNIQUE, value BLOB NOT NULL);
"""


@dataclass
class Change:
    added: int = 0
    changed: int = 0
    removed: int = 0
    kept: int = 0
    renamed: bool = False


def key_list(*texts: str) -> list[str]:
    return [key for text in texts for word in words(text) if (key := sound_key(word))]


def name_fields(names: dict) -> tuple[str, str, str]:
    """A talk's names as the index holds them: (given, auto, file)."""
    return tuple(str(names.get(field) or "").strip() for field in ("given", "auto", "file"))


def unit(values, dims: int) -> np.ndarray:
    vector = np.asarray(values, dtype=np.float32).reshape(-1)
    if vector.shape[0] != dims:
        raise ValueError(f"expected {dims} numbers, got {vector.shape[0]}")
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm > 0 else vector


class VectorTable:
    """Unit vectors in one float16 matrix. A removed row's slot is reused by the next vector added."""

    def __init__(self, dims: int, block: int = 8192):
        self.dims, self.block = dims, block
        self.matrix = np.zeros((64, dims), dtype=np.float16)
        self.alive = np.zeros(64, dtype=bool)
        self.keys: list[str | None] = []
        self.slots: dict[str, int] = {}
        self.free: list[int] = []

    @property
    def rows(self) -> int:
        """Rows ever used; it grows only when no freed row is left."""
        return len(self.keys)

    def __len__(self) -> int:
        return len(self.slots)

    def add(self, key: str, values) -> int:
        vector = unit(values, self.dims)
        slot = self.slots.get(key)
        if slot is None:
            if self.free:
                slot = self.free.pop()
            else:
                slot = len(self.keys)
                self.keys.append(None)
                if slot >= len(self.matrix):
                    self.matrix = np.concatenate([self.matrix, np.zeros_like(self.matrix)])
                    self.alive = np.concatenate([self.alive, np.zeros_like(self.alive)])
            self.slots[key], self.keys[slot] = slot, key
        self.matrix[slot], self.alive[slot] = vector, True
        return slot

    def remove(self, key: str) -> bool:
        slot = self.slots.pop(key, None)
        if slot is None:
            return False
        self.keys[slot], self.alive[slot] = None, False
        self.matrix[slot] = 0
        self.free.append(slot)
        return True

    def nearest(self, values, k: int) -> list[tuple[str, float]]:
        """The `k` closest vectors by cosine similarity, closest first. Exact: every row is compared, in blocks."""
        if not self.slots or k <= 0:
            return []
        query, used = unit(values, self.dims), len(self.keys)
        similarity = np.empty(used, dtype=np.float32)
        for low in range(0, used, self.block):
            high = min(used, low + self.block)
            similarity[low:high] = self.matrix[low:high].astype(np.float32) @ query
        similarity[~self.alive[:used]] = -np.inf
        k = min(k, len(self.slots))
        top = np.argpartition(-similarity, k - 1)[:k]
        top = top[np.argsort(-similarity[top], kind="stable")]
        return [(self.keys[slot], float(similarity[slot])) for slot in top]


@dataclass
class Query:
    terms: list[str]
    live: bool
    texts: list[str]  # FTS phrase for each term, as written
    keys: list[str]  # FTS alternatives for each term's spelling key ("" when it has none)
    marks: set[str]  # spelling keys a word may have to be marked in a snippet
    key_prefix: str = ""

    @property
    def gujarati(self) -> bool:
        return any(GUJARATI.search(term) for term in self.terms)

    def mark(self, word: str) -> int:
        """2 for a word as written, 1 for a spelling match, 0 otherwise."""
        word = fold(word)
        if word in self.terms or (self.live and word.startswith(self.terms[-1])):
            return 2
        if any(len(term) >= 4 and word.startswith(term) and len(word) - len(term) <= 3 for term in self.terms):
            return 2  # an inflection the stemmer matched: "services" for "service"
        key = sound_key(word)
        return int(bool(key) and (key in self.marks or bool(self.key_prefix and key.startswith(self.key_prefix))))


def any_of(columns: str, phrases: list[str]) -> str:
    phrases = [phrase for phrase in phrases if phrase]
    return f"{columns} : ({' OR '.join(phrases)})" if phrases else ""


def all_of(columns: str, query: Query) -> str:
    groups = []
    for text, keys in zip(query.texts, query.keys):
        group = f"{columns} : {text}" + (f" OR keys : {keys}" if keys else "")
        groups.append(f"({group})")
    return " AND ".join(groups)


class TalkIndex:
    def __init__(self, model: str, dims: int, *, empty: bool = False):
        self.model, self.dims = model, dims
        self.lock = threading.RLock()
        self.db = sqlite3.connect(":memory:", check_same_thread=False)
        # An index dropped without close() still frees its database, but not at interpreter exit: a search worker may
        # still be inside SQLite then, and closing the database under it crashes Python. The process frees it anyway.
        weakref.finalize(self, self.db.close).atexit = False
        self.vectors = VectorTable(dims)
        if not empty:
            self.db.executescript(SCHEMA)
            with self.db:
                self.db.executemany("INSERT INTO meta(key, value) VALUES (?, ?)", self._meta().items())

    def check(self) -> None:
        """Raise sqlite3.DatabaseError unless the database and both word indexes are whole and agree."""
        with self.lock:
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("the search index is damaged")
            with self.db:
                self.db.execute("INSERT INTO texts(texts) VALUES ('integrity-check')")
                self.db.execute("INSERT INTO titles(titles) VALUES ('integrity-check')")

    def close(self) -> None:
        with self.lock:
            self.db.close()

    def _meta(self) -> dict[str, str]:
        return {"schema": SCHEMA_VERSION, "model": self.model, "dims": str(self.dims)}

    # ----- saving -----

    def save(self, path: Path) -> None:
        """Write the whole index to `path` (a new local file) through SQLite's backup API."""
        with self.lock:
            target = sqlite3.connect(str(path))
            try:
                self.db.backup(target)
            finally:
                target.close()

    @classmethod
    def load(cls, path: Path, model: str, dims: int) -> TalkIndex:
        """An index saved by `save`. Raises ValueError for another schema or embedding model and
        sqlite3.DatabaseError for a damaged file."""
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(path)
        index = cls(model, dims, empty=True)
        try:
            source = sqlite3.connect(str(path))
            try:
                source.backup(index.db)
            finally:
                source.close()
            found = dict(index.db.execute("SELECT key, value FROM meta"))
            if found != index._meta():
                raise ValueError(f"the saved search index was built for {found}, not {index._meta()}")
            index.check()
            for key, value in index.db.execute("SELECT key, value FROM vectors"):
                if not isinstance(value, bytes) or len(value) != dims * 2:
                    raise ValueError(f"a saved embedding is not {dims} numbers")
                index.vectors.add(key, np.frombuffer(value, dtype=np.float16))
        except BaseException:
            index.close()
            raise
        return index

    # ----- writing -----

    def vector_key(self, text: str) -> str:
        return hashlib.sha256(f"{self.model}\n{self.dims}\n{text}".encode("utf-8")).hexdigest()[:32]

    def talks(self) -> dict[str, str]:
        """Every indexed talk and the stamp it was indexed with."""
        with self.lock:
            return dict(self.db.execute("SELECT talk, stamp FROM talks"))

    def names(self) -> dict[str, tuple[str, str, str]]:
        """Every indexed talk's names as held: (given, auto, file), compared with `name_fields`."""
        with self.lock:
            return {talk: names for talk, *names in self.db.execute("SELECT talk, given, auto, file FROM talks")}

    def put_talk(self, talk: str, names: dict, passages: list[Passage], stamp: str = "") -> Change:
        """Make the index hold exactly these passages and names for `talk`, touching only what differs."""
        change, uses, released = Change(), Counter(), set()
        with self.lock:
            with self.db:
                row = self.db.execute("SELECT id FROM talks WHERE talk = ?", (talk,)).fetchone()
                talk_id = row[0] if row else self.db.execute("INSERT INTO talks(talk) VALUES (?)", (talk,)).lastrowid
                change.renamed = self._write_names(talk_id, names, uses)
                old = {
                    (kind, n): (passage_id, digest)
                    for passage_id, kind, n, digest in self.db.execute(
                        "SELECT id, kind, n, digest FROM passages WHERE talk = ?", (talk_id,)
                    )
                }
                seen = set()
                for passage in passages:
                    slot = (passage.kind, passage.n)
                    if slot in seen:
                        continue
                    seen.add(slot)
                    prior = old.get(slot)
                    if prior and prior[1] == passage.digest:
                        change.kept += 1
                        continue
                    if prior:
                        self._delete_passage(prior[0], uses, released)
                        change.changed += 1
                    else:
                        change.added += 1
                    self._insert_passage(talk_id, passage, uses, prior[0] if prior else None)
                for slot in old.keys() - seen:
                    self._delete_passage(old[slot][0], uses, released)
                    change.removed += 1
                self._count_keys(uses)
                self.db.execute("UPDATE talks SET stamp = ? WHERE id = ?", (stamp, talk_id))
                freed = self._unreferenced(released)
            for key in freed:
                self.vectors.remove(key)
        return change

    def rename(self, talk: str, names: dict) -> bool | None:
        """Change only a talk's names: True when they changed, False when they were these already, None when the
        talk is not indexed."""
        uses = Counter()
        with self.lock, self.db:
            row = self.db.execute("SELECT id FROM talks WHERE talk = ?", (talk,)).fetchone()
            if not row:
                return None
            changed = self._write_names(row[0], names, uses)
            self._count_keys(uses)
            return changed

    def drop_talk(self, talk: str) -> bool:
        uses, released = Counter(), set()
        with self.lock:
            with self.db:
                row = self.db.execute("SELECT id, keys FROM talks WHERE talk = ?", (talk,)).fetchone()
                if not row:
                    return False
                talk_id, keys = row
                for (passage_id,) in self.db.execute("SELECT id FROM passages WHERE talk = ?", (talk_id,)).fetchall():
                    self._delete_passage(passage_id, uses, released)
                uses.subtract(keys.split())
                self.db.execute("DELETE FROM titles WHERE rowid = ?", (talk_id,))
                self.db.execute("DELETE FROM talks WHERE id = ?", (talk_id,))
                self._count_keys(uses)
                freed = self._unreferenced(released)
            for key in freed:
                self.vectors.remove(key)
        return True

    def missing_vectors(self, limit: int = 100) -> list[tuple[str, str]]:
        """(vector key, text to embed) for passages whose text has no embedding yet, each text once."""
        with self.lock:
            rows = self.db.execute(
                "SELECT vec, kind, label, gu, en, sm, MIN(id) AS first FROM passages "
                "WHERE vec IS NOT NULL AND NOT EXISTS (SELECT 1 FROM vectors WHERE vectors.key = passages.vec) "
                "GROUP BY vec ORDER BY first LIMIT ?",
                (limit,),
            ).fetchall()
        return [(key, meaning_text(kind, label, gu, en, sm)) for key, kind, label, gu, en, sm, _ in rows]

    def put_vectors(self, vectors: dict[str, list[float]]) -> int:
        """Store embeddings by vector key. Ones no passage needs any more (it changed meanwhile) are skipped."""
        stored = []
        with self.lock:
            with self.db:
                for key, values in vectors.items():
                    try:
                        vector = unit(values, self.dims)
                    except ValueError:
                        continue
                    if not self.db.execute("SELECT 1 FROM passages WHERE vec = ? LIMIT 1", (key,)).fetchone():
                        continue
                    blob = vector.astype(np.float16).tobytes()
                    self.db.execute("INSERT OR REPLACE INTO vectors(key, value) VALUES (?, ?)", (key, blob))
                    stored.append((key, vector))
            for key, vector in stored:
                self.vectors.add(key, vector)
        return len(stored)

    def stats(self) -> dict:
        with self.lock:
            one = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
            return {
                "talks": one("SELECT COUNT(*) FROM talks"),
                "passages": one("SELECT COUNT(*) FROM passages"),
                "vectors": len(self.vectors),
                "bytes": one("PRAGMA page_count") * one("PRAGMA page_size") + self.vectors.matrix.nbytes,
            }

    def _write_names(self, talk_id: int, names: dict, uses: Counter) -> bool:
        given, auto, file = name_fields(names)
        old = self.db.execute("SELECT given, auto, file, keys FROM talks WHERE id = ?", (talk_id,)).fetchone()
        if old[:3] == (given, auto, file):
            return False
        keys = sorted(set(key_list(given, auto, file)))
        uses.subtract(old[3].split())
        uses.update(keys)
        self.db.execute(
            "UPDATE talks SET given = ?, auto = ?, file = ?, keys = ? WHERE id = ?",
            (given, auto, file, " ".join(keys), talk_id),
        )
        self.db.execute("DELETE FROM titles WHERE rowid = ?", (talk_id,))
        self.db.execute(
            "INSERT INTO titles(rowid, given, auto, file, keys) VALUES (?, ?, ?, ?, ?)",
            (talk_id, given, auto, file, " ".join(keys)),
        )
        return True

    def _insert_passage(self, talk_id: int, passage: Passage, uses: Counter, passage_id: int | None) -> None:
        text = passage.meaning_text()
        keys = " ".join(key_list(passage.gu, passage.gl, passage.en, passage.sm))  # every word's key, for BM25
        passage_id = self.db.execute(
            "INSERT INTO passages(id, talk, kind, n, start, stop, seg, label, gu, gl, en, sm, lines, keys, digest, vec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                passage_id, talk_id, passage.kind, passage.n, passage.start, passage.end, passage.seg, passage.label,
                passage.gu, passage.gl, passage.en, passage.sm, json.dumps(passage.lines, separators=(",", ":")),
                keys, passage.digest,
                self.vector_key(text) if text else None,
            ),
        ).lastrowid  # fmt: skip
        uses.update(set(keys.split()))
        self.db.execute(
            "INSERT INTO texts(rowid, gu, gl, en, sm, keys) VALUES (?, ?, ?, ?, ?, ?)",
            (passage_id, passage.gu, passage.gl, passage.en, passage.sm, keys),
        )

    def _delete_passage(self, passage_id: int, uses: Counter, released: set) -> None:
        gu, gl, en, sm, keys, vec = self.db.execute(
            "SELECT gu, gl, en, sm, keys, vec FROM passages WHERE id = ?", (passage_id,)
        ).fetchone()
        uses.subtract(set(keys.split()))
        if vec:
            released.add(vec)
        # `texts` indexes the passages table without a copy of its own, so it is told exactly what it indexed.
        self.db.execute(
            "INSERT INTO texts(texts, rowid, gu, gl, en, sm, keys) VALUES ('delete', ?, ?, ?, ?, ?, ?)",
            (passage_id, gu, gl, en, sm, keys),
        )
        self.db.execute("DELETE FROM passages WHERE id = ?", (passage_id,))

    def _count_keys(self, uses: Counter) -> None:
        """Apply changes in how many rows use each spelling key; keep `near` in step with keys first used or gone."""
        for key, change in uses.items():
            if not change:
                continue
            row = self.db.execute("SELECT uses FROM vocab WHERE key = ?", (key,)).fetchone()
            total = (row[0] if row else 0) + change
            if total > 0:
                self.db.execute("INSERT OR REPLACE INTO vocab(key, uses) VALUES (?, ?)", (key, total))
                if not row and len(key) >= NEAR_MIN:
                    variants = [(variant, key) for variant in edit_variants(key)]
                    self.db.executemany("INSERT OR IGNORE INTO near(variant, key) VALUES (?, ?)", variants)
            elif row:
                self.db.execute("DELETE FROM vocab WHERE key = ?", (key,))
                if len(key) >= NEAR_MIN:
                    variants = [(variant, key) for variant in edit_variants(key)]
                    self.db.executemany("DELETE FROM near WHERE variant = ? AND key = ?", variants)

    def _unreferenced(self, released: set) -> list[str]:
        freed = []
        for key in released:
            if not self.db.execute("SELECT 1 FROM passages WHERE vec = ? LIMIT 1", (key,)).fetchone():
                self.db.execute("DELETE FROM vectors WHERE key = ?", (key,))
                freed.append(key)
        return freed

    # ----- searching -----

    def _near(self, key: str) -> set[str]:
        if len(key) < NEAR_MIN:
            return set()
        variants = sorted(edit_variants(key))
        rows = self.db.execute(
            f"SELECT DISTINCT key FROM near WHERE variant IN ({','.join('?' * len(variants))})", variants
        )
        return {found for (found,) in rows if found != key and similar(key, found)}

    def _query(self, text: str, live: bool) -> Query | None:
        terms = list(dict.fromkeys(words(text)))
        if not terms:
            return None
        texts, keys, marks, key_prefix = [], [], set(), ""
        for at, term in enumerate(terms):
            last = live and at == len(terms) - 1
            texts.append(f'"{term}"*' if last and len(term) >= 2 else f'"{term}"')
            key = sound_key(term)
            if len(key) < min(2, len(term)):
                key = ""  # "zzzz" folds to "j": a key that short would match every જ
            alternatives = sorted({key} | self._near(key)) if key else []
            marks.update(alternatives)
            phrases = [f'"{alternative}"' for alternative in alternatives]
            if last and len(key) >= 2:
                phrases.append(f'"{key}"*')
                key_prefix = key
            keys.append(f"({' OR '.join(phrases)})" if phrases else "")
        return Query(terms, live, texts, keys, marks, key_prefix)

    def _ranked(self, table: str, expression: str, limit: int = LIST_LIMIT) -> list[int]:
        if not expression:
            return []
        try:
            rows = self.db.execute(
                f"SELECT rowid FROM {table} WHERE {table} MATCH ? ORDER BY rank LIMIT ?", (expression, limit)
            )
            return [rowid for (rowid,) in rows]
        except sqlite3.OperationalError:
            return []  # cannot happen with quoted tokenizer output; never let a query fail the page

    def nearest(self, vector, k: int = MEANING_K) -> list[tuple[int, float]]:
        """(passage id, cosine similarity) for the passages whose embeddings are closest to `vector`."""
        with self.lock:
            pairs = dict(self.vectors.nearest(vector, k))
            if not pairs:
                return []
            marks = ",".join("?" * len(pairs))
            rows = self.db.execute(f"SELECT id, vec FROM passages WHERE vec IN ({marks})", list(pairs)).fetchall()
        return sorted(((passage_id, pairs[key]) for passage_id, key in rows), key=lambda pair: -pair[1])

    def _meaning(self, vector) -> list[int]:
        if vector is None or not len(self.vectors):
            return []
        try:
            found = self.nearest(vector)
        except ValueError:
            return []  # a vector of another size: meaning search is skipped, words still answer
        if not found:
            return []
        floor = max(MEANING_FLOOR, found[0][1] - MEANING_BAND)
        return [passage_id for passage_id, similarity in found if similarity >= floor]

    def _fuse(self, query: Query, vector) -> tuple[list[tuple[int, int, float, list[str]]], dict[int, tuple]]:
        """Passages as (id, tier, score, why) and talk name matches as {talk id: (tier, score)}.
        Tier 0 holds rows matching every word (as written or by spelling); tier 1 the rest. With several words, the rows
        matching all of them are a ranked list of their own, so none is lost behind rows that repeat one word often."""
        texts = "{gu gl en sm}"
        lists = {
            "words": self._ranked("texts", any_of(texts, query.texts)),
            "spelling": self._ranked("texts", any_of("keys", query.keys)),
            "meaning": self._meaning(vector),
        }
        if len(query.terms) > 1:
            lists["every"] = self._ranked("texts", all_of(texts, query))
            every = set(lists["every"])
        else:
            every = set(lists["words"]) | set(lists["spelling"])
        scores, why = defaultdict(float), defaultdict(list)
        for name, ranked in lists.items():
            reason = "words" if name == "every" else name  # matching all the words is a match by words
            for rank, passage_id in enumerate(ranked):
                scores[passage_id] += WEIGHTS[name] / (RRF_K + rank + 1)
                if reason not in why[passage_id]:
                    why[passage_id].append(reason)
        passages = [(pid, 0 if pid in every else 1, score, why[pid]) for pid, score in scores.items()]
        titles = "{given auto file}"
        named = {
            "words": self._ranked("titles", any_of(titles, query.texts)),
            "spelling": self._ranked("titles", any_of("keys", query.keys)),
        }
        if len(query.terms) > 1:
            named["every"] = self._ranked("titles", all_of(titles, query))
            every_name = set(named["every"])
        else:
            every_name = set(named["words"]) | set(named["spelling"])
        name_scores = defaultdict(float)
        for name, ranked in named.items():
            for rank, talk_id in enumerate(ranked):
                name_scores[talk_id] += NAME_WEIGHTS[name] / (RRF_K + rank + 1)
        names = {talk_id: (0 if talk_id in every_name else 1, score) for talk_id, score in name_scores.items()}
        return passages, names

    def _rows(self, passage_ids) -> dict[int, tuple]:
        passage_ids = list(passage_ids)
        rows = {}
        for low in range(0, len(passage_ids), 500):
            chunk = passage_ids[low : low + 500]
            for row in self.db.execute(
                "SELECT p.id, p.talk, t.talk, p.kind, p.n, p.start, p.stop, p.seg, p.label, p.gu, p.gl, p.en, p.sm, p.lines "
                f"FROM passages p JOIN talks t ON t.id = p.talk WHERE p.id IN ({','.join('?' * len(chunk))})",
                chunk,
            ):
                rows[row[0]] = row
        return rows

    def _talk_names(self, talk_ids) -> dict[int, tuple[str, str, str]]:
        talk_ids = list(talk_ids)
        marks = ",".join("?" * len(talk_ids))
        rows = self.db.execute(f"SELECT id, talk, given, auto, file FROM talks WHERE id IN ({marks})", talk_ids)
        return {row[0]: (row[1], row[2] or row[3] or row[4], row[4]) for row in rows}

    def find(self, text: str, *, live: bool = False, vector=None, talks: int = 20, per_talk: int = 3) -> dict:
        """Talks matching `text`, best first, each with up to `per_talk` moments and a count of the rest.
        `live` treats the last word as a prefix (search while typing); `vector` adds meaning search."""
        with self.lock:
            query = self._query(text, live)
            if query is None:
                return {"talks": [], "total": 0}
            passages, names = self._fuse(query, vector)
            rows = self._rows(pid for pid, *_ in passages)
            by_talk: dict[int, list] = defaultdict(list)
            for passage_id, tier, score, why in passages:
                if passage_id in rows:
                    by_talk[rows[passage_id][1]].append((tier, -score, passage_id, why))
            ranking = []
            for talk_id in by_talk.keys() | names.keys():
                hits = sorted(by_talk.get(talk_id, []))
                best_tier, best_score = (hits[0][0], -hits[0][1]) if hits else (1, 0.0)
                name_tier, name_score = names.get(talk_id, (1, 0.0))
                ranking.append((min(best_tier, name_tier), -(best_score + name_score), talk_id, hits))
            ranking.sort()
            labels = self._talk_names(talk_id for _, _, talk_id, _ in ranking[:talks])
            found = []
            for _, _, talk_id, hits in ranking[:talks]:
                talk, name, file = labels[talk_id]
                found.append({
                    "talk": talk, "name": name, "file": file, "name_hit": talk_id in names,
                    "hits": [self._hit(rows[pid], why, query) for _, _, pid, why in hits[:per_talk]],
                    "more": max(0, len(hits) - per_talk),
                })  # fmt: skip
            return {"talks": found, "total": len(ranking)}

    def context(self, text: str, vector=None, *, limit: int = 10, per_talk: int = 4, chars: int = 1200) -> list[dict]:
        """The passages that best answer a question, at most `per_talk` from one talk, for a model to read."""
        with self.lock:
            query = self._query(text, False)
            if query is None:
                return []
            passages, _ = self._fuse(query, vector)
            passages.sort(key=lambda item: -item[2])
            rows = self._rows(pid for pid, *_ in passages[: limit * per_talk * 4])
            labels = self._talk_names({row[1] for row in rows.values()}) if rows else {}
            chosen, per = [], Counter()
            for passage_id, *_ in passages:
                row = rows.get(passage_id)
                if row is None or per[row[1]] >= per_talk:
                    continue
                per[row[1]] += 1
                _, talk_id, talk, kind, n, start, _, seg, label, gu, _, en, sm, _ = row
                chosen.append({
                    "talk": talk, "name": labels[talk_id][1], "kind": kind, "n": n, "start": start, "seg": seg,
                    "label": label, "gu": gu[:chars], "en": en[:chars], "sm": sm[:chars],
                })  # fmt: skip
                if len(chosen) == limit:
                    break
            return chosen

    def _hit(self, row: tuple, why: list[str], query: Query) -> dict:
        _, _, _, kind, n, start, stop, seg, label, gu, gl, en, sm, lines = row
        columns = {"gu": gu, "gl": gl, "en": en, "sm": sm}
        snippets = {column: snippet(value, query) for column, value in columns.items() if value}
        best = max((score for score, *_ in snippets.values()), default=0)
        if best:
            order = ("gu", "gl", "en", "sm") if query.gujarati else ("gl", "en", "gu", "sm")
        else:
            order = ("gu", "en", "sm", "gl") if query.gujarati else ("en", "gl", "gu", "sm")
        column = next((c for c in order if c in snippets and snippets[c][0] == best), "gu")
        if best and kind == "line" and lines:  # open at the line with the first match, not the passage's first line
            seg, start = line_at(json.loads(lines), column, snippets[column][2]) or (seg, start)
        return {
            "kind": kind, "n": n, "start": start, "end": stop, "seg": seg, "label": label, "column": column,
            "parts": snippets[column][1] if column in snippets else [], "why": why,
        }  # fmt: skip


def snippet(text: str, query: Query) -> tuple[int, list[list], int]:
    """(how well `text` matches, its parts as [text, marked] pairs, where the first match starts or -1), cut to about
    SNIPPET_WORDS words around the first match. Joined, the parts give the original text (with "…" where it was cut)."""
    spans = [(match.start(), match.end(), query.mark(match.group())) for match in WORD.finditer(text)]
    score = sum(mark for *_, mark in spans)
    first_at = next((start for start, _, mark in spans if mark), -1)
    begin, end, lead, tail, window = 0, len(text), "", "", spans
    if len(spans) > SNIPPET_WORDS + 6:
        first = next((at for at, (*_, mark) in enumerate(spans) if mark), 0)
        low = max(0, min(first - SNIPPET_LEAD, len(spans) - SNIPPET_WORDS))
        high = min(len(spans), low + SNIPPET_WORDS)
        window = spans[low:high]
        begin, end = window[0][0], window[-1][1]
        lead, tail = ("…" if low > 0 else ""), ("…" if high < len(spans) else "")
    parts: list[list] = []

    def push(piece: str, marked: bool) -> None:
        if piece:
            if parts and parts[-1][1] == marked:
                parts[-1][0] += piece
            else:
                parts.append([piece, marked])

    push(lead, False)
    at = begin
    for start, stop, mark in window:
        push(text[at:start], False)
        push(text[start:stop], bool(mark))
        at = stop
    push(text[at:end], False)
    push(tail, False)
    return score, parts, first_at
