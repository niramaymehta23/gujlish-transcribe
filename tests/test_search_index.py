"""The search index on its own: passages cut from a talk, word and spelling ranking, meaning search over stored
vectors, incremental updates, snapshots, and the Gemini requests for embeddings and answers (with HTTP stubbed)."""

import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from transcriber.english.llm import Completion, LLMError
from web.search import gemini
from web.search.index import TalkIndex, VectorTable
from web.search.passages import Passage, build_passages


def line(n, gu="", gl="", en="", start=None):
    start = n * 40.0 if start is None else start
    return Passage(kind="line", n=n, start=start, end=start + 10, seg=n, gu=gu, gl=gl, en=en)


NAMES = {"given": "", "auto": "", "file": "talk.mp3"}


def put(index, talk, passages, **names):
    return index.put_talk(talk, {**NAMES, **names}, passages, stamp="s1")


def hits(result, talk=None):
    for found in result["talks"]:
        if talk is None or found["talk"] == talk:
            return found["hits"]
    return []


class PassagesTest(unittest.TestCase):
    SEGMENTS = [
        {"start": 0.0, "end": 9.0, "text": "સ્વામીજી આવ્યા"},
        {"start": 10.0, "end": 20.0, "text": "સેવા કરો"},
        {"start": 35.0, "end": 40.5, "text": "ભક્તિ"},
        {"start": 41.0, "end": 50.0, "text": "દર્શન કરો"},
        {"start": 79.0, "end": 84.0, "text": ""},
        {"start": 85.0, "end": 90.0, "text": "વાત પૂરી"},
    ]

    def test_lines_are_grouped_into_forty_second_passages_by_start_time(self):
        passages = build_passages(self.SEGMENTS)
        self.assertEqual([(p.kind, p.n, p.seg, p.start, p.end) for p in passages], [
            ("line", 0, 0, 0.0, 40.5), ("line", 1, 3, 41.0, 50.0), ("line", 2, 5, 85.0, 90.0),
        ])  # fmt: skip
        self.assertEqual(passages[0].gu, "સ્વામીજી આવ્યા સેવા કરો ભક્તિ")

    def test_gujlish_and_english_follow_their_segments(self):
        gujlish = [["Swamiji", "aavya"], ["seva", "karo"], ["bhakti"], ["darshan", "karo"], [], ["vaat", "puri"]]
        english = ["Swamiji came", None, "Devotion", "Take darshan", None, "The talk ends"]
        first = build_passages(self.SEGMENTS, gujlish, english)[0]
        self.assertEqual(first.gl, "Swamiji aavya seva karo bhakti")
        self.assertEqual(first.en, "Swamiji came Devotion")
        self.assertEqual(first.meaning_text(), "સ્વામીજી આવ્યા સેવા કરો ભક્તિ\nSwamiji came Devotion")  # no Gujlish

    def test_a_word_fix_changes_only_its_passage_and_moves_no_boundary(self):
        before = build_passages(self.SEGMENTS)
        fixed = [dict(segment) for segment in self.SEGMENTS]
        fixed[3]["text"] = "દર્શન કર્યા"
        after = build_passages(fixed)
        self.assertEqual([(p.n, p.start) for p in before], [(p.n, p.start) for p in after])
        self.assertEqual([b.digest == a.digest for b, a in zip(before, after)], [True, False, True])

    def test_summary_points_carry_their_moment_and_label(self):
        summary = {
            "title": "Seva", "one_line": "On service.", "overview": "Swamiji speaks about seva.",
            "themes": [{"theme": "Seva", "points": [{"point": "Serve quietly", "t": "00:41"}, {"point": "Untimed"}]}],
            "stories": [{"title": "The well", "what_happened": "A boy dug a well", "start": "01:25"}],
            "quotes": [{"quote": "Seva is bhakti", "t": "00:12"}],
            "asks": [{"item": "Come on Sunday", "t": "bad"}],
        }  # fmt: skip
        points = [p for p in build_passages(self.SEGMENTS, summary=summary) if p.kind != "line"]
        self.assertEqual([(p.kind, p.label, p.sm, p.start, p.seg) for p in points], [
            ("summary", "Summary", "On service. Swamiji speaks about seva.", None, None),
            ("point", "Seva", "Serve quietly", 41.0, 3),
            ("point", "Seva", "Untimed", None, None),
            ("point", "Story: The well", "A boy dug a well", 85.0, 5),
            ("point", "Quote", "Seva is bhakti", 10.0, 1),
            ("point", "Ask", "Come on Sunday", None, None),
        ])  # fmt: skip
        self.assertEqual(points[1].meaning_text(), "Seva: Serve quietly")


class WordSearchTest(unittest.TestCase):
    def setUp(self):
        self.index = TalkIndex(model="test-embed", dims=4)
        self.addCleanup(self.index.close)

    def test_a_latin_query_finds_a_passage_written_only_in_gujarati(self):
        put(self.index, "t1", [line(0, gu="સ્વામીજી સેવા કરે છે")])
        found = hits(self.index.find("swamiji"))
        self.assertEqual(len(found), 1)
        self.assertIn("spelling", found[0]["why"])
        self.assertEqual(found[0]["column"], "gu")
        self.assertIn(["સ્વામીજી", True], found[0]["parts"])

    def test_a_typo_still_finds_the_word(self):
        put(self.index, "t1", [line(0, gl="Swamiji aaje aavya")])
        found = hits(self.index.find("swamjii"))
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["why"], ["spelling"])

    def test_words_as_written_outrank_spelling_matches(self):
        put(self.index, "t1", [line(0, gu="સ્વામીજી આવ્યા સ્વામીજી"), line(1, gl="swamiji aavya")])
        self.assertEqual([hit["n"] for hit in hits(self.index.find("swamiji"))], [1, 0])

    def test_more_occurrences_rank_higher(self):
        put(self.index, "t1", [line(0, gl="seva darshan bhakti"), line(1, gl="seva seva darshan")])
        self.assertEqual([hit["n"] for hit in hits(self.index.find("seva"))], [1, 0])

    def test_passages_with_every_word_come_first_then_any_word(self):
        put(self.index, "t1", [line(0, gl="seva karvi"), line(1, gl="vaat kari"), line(2, gl="seva ni vaat")])
        found = hits(self.index.find("seva vaat"))
        self.assertEqual(found[0]["n"], 2)
        self.assertEqual(sorted(hit["n"] for hit in found), [0, 1, 2])

    def test_while_typing_the_last_word_is_a_prefix(self):
        put(self.index, "t1", [line(0, gl="Swamiji aavya")])
        self.assertEqual(len(hits(self.index.find("swa", live=True))), 1)
        self.assertEqual(self.index.find("swa")["talks"], [])

    def test_talk_names_match_in_any_script(self):
        put(self.index, "t1", [line(0, gl="seva")], given="Diwali katha")
        put(self.index, "t2", [line(0, gl="seva")], auto="દિવાળી નો સત્સંગ")
        put(self.index, "t3", [line(0, gl="seva")], file="diwali-2026.mp3")
        put(self.index, "t4", [line(0, gl="seva")], given="Holi")
        talks = self.index.find("diwali")["talks"]
        self.assertEqual(sorted(talk["talk"] for talk in talks), ["t1", "t2", "t3"])
        self.assertTrue(all(talk["name_hit"] for talk in talks))
        self.assertEqual(next(t for t in talks if t["talk"] == "t1")["name"], "Diwali katha")

    def test_snippets_mark_the_matching_words_in_the_column_that_matched(self):
        put(self.index, "t1", [line(0, gu="સ્વામીજી સેવા કરે", gl="Swamiji seva kare", en="Swamiji does service")])
        gujlish = hits(self.index.find("seva"))[0]
        self.assertEqual(gujlish["column"], "gl")
        self.assertEqual(gujlish["parts"], [["Swamiji ", False], ["seva", True], [" kare", False]])
        gujarati = hits(self.index.find("સેવા"))[0]
        self.assertEqual(gujarati["column"], "gu")
        self.assertEqual(gujarati["parts"], [["સ્વામીજી ", False], ["સેવા", True], [" કરે", False]])
        english = hits(self.index.find("service"))[0]
        self.assertEqual(english["column"], "en")
        self.assertEqual(english["parts"][-1], ["service", True])

    def test_a_hit_opens_at_the_line_holding_the_first_match_in_any_script(self):
        segments = [
            {"start": 0.0, "end": 9.0, "text": "સ્વામીજી આવ્યા"},
            {"start": 10.0, "end": 20.0, "text": "સેવા કરો"},
            {"start": 35.0, "end": 40.5, "text": "ભક્તિ દર્શન"},
        ]
        gujlish = [["Swamiji", "aavya"], [], ["bhakti", "darshan"]]
        english = ["Swamiji came", "Do service", None]
        put(self.index, "t1", build_passages(segments, gujlish, english))
        for query, column, seg, start in (
            ("સ્વામીજી", "gu", 0, 0.0), ("દર્શન", "gu", 2, 35.0), ("darshan", "gl", 2, 35.0), ("service", "en", 1, 10.0),
        ):  # fmt: skip
            with self.subTest(query):
                hit = hits(self.index.find(query))[0]
                self.assertEqual((hit["column"], hit["seg"], hit["start"]), (column, seg, start))

    def test_a_passage_with_every_word_is_never_lost_behind_ones_repeating_one_word(self):
        for n in range(320):
            put(self.index, f"seva{n}", [line(0, gl="seva seva seva")])
            put(self.index, f"vaat{n}", [line(0, gl="vaat vaat vaat")])
        put(self.index, "both", [line(0, gl=" ".join(["katha"] * 30 + ["seva"] + ["katha"] * 30 + ["vaat"]))])
        found = self.index.find("seva vaat")["talks"]
        self.assertEqual(found[0]["talk"], "both")
        self.assertEqual(found[0]["hits"][0]["why"][0], "words")

    def test_long_passages_are_cut_around_the_first_match(self):
        text = " ".join(["vaat"] * 60 + ["darshan"] + ["vaat"] * 60)
        put(self.index, "t1", [line(0, gl=text)])
        parts = hits(self.index.find("darshan"))[0]["parts"]
        self.assertTrue(parts[0][0].startswith("…") and parts[-1][0].endswith("…"))
        self.assertLess(len("".join(text for text, _ in parts).split()), 40)

    def test_each_talk_shows_three_moments_and_counts_the_rest(self):
        put(self.index, "t1", [line(n, gl=f"seva {n}") for n in range(5)])
        talk = self.index.find("seva")["talks"][0]
        self.assertEqual((len(talk["hits"]), talk["more"]), (3, 2))

    def test_a_long_word_folding_to_one_letter_matches_nothing_by_spelling(self):
        put(self.index, "t1", [line(0, gu="એ જ વાત", gl="e j vaat")])
        self.assertEqual(self.index.find("zzzz")["talks"], [])
        self.assertEqual(len(hits(self.index.find("જ"))), 1)

    def test_query_text_cannot_break_the_search_syntax(self):
        put(self.index, "t1", [line(0, gl="seva NEAR vaat")])
        for query in ['"', "seva AND", "NOT", "*", "seva)", "col:seva", "^seva", ""]:
            self.index.find(query)  # no sqlite3.OperationalError

    def test_summary_points_are_searchable(self):
        summary = [Passage(kind="point", n=0, start=None, end=None, seg=None, label="Seva", sm="Serve quietly")]
        put(self.index, "t1", summary)
        found = hits(self.index.find("quietly"))
        self.assertEqual((found[0]["kind"], found[0]["column"], found[0]["label"]), ("point", "sm", "Seva"))


def fake_vector(text):
    """Four "meanings": devotion, food, travel, other. Shares no words with the queries that use it."""
    concepts = [("ભક્તિ", "devotion", "god"), ("ભોજન", "food", "cooking"), ("યાત્રા", "travel", "journey")]
    vector = [float(any(word in text for word in group)) for group in concepts]
    return vector + [0.0 if any(vector) else 1.0]


class MeaningSearchTest(unittest.TestCase):
    def setUp(self):
        self.index = TalkIndex(model="test-embed", dims=4)
        self.addCleanup(self.index.close)

    def fill(self):
        missing = self.index.missing_vectors()
        self.index.put_vectors({key: fake_vector(text) for key, text in missing})
        return len(missing)

    def test_a_query_sharing_no_words_finds_the_passage_by_meaning(self):
        put(self.index, "t1", [line(0, gu="ભગવાનની ભક્તિ"), line(1, gu="ભોજન બનાવ્યું")])
        self.assertEqual(self.fill(), 2)
        found = hits(self.index.find("love for the almighty", vector=[0.9, 0.1, 0, 0]))
        self.assertEqual([(hit["n"], hit["why"]) for hit in found], [(0, ["meaning"])])

    def test_without_a_query_vector_only_words_are_searched(self):
        put(self.index, "t1", [line(0, gu="ભગવાનની ભક્તિ")])
        self.fill()
        self.assertEqual(self.index.find("love for the almighty")["talks"], [])

    def test_a_changed_passage_needs_one_new_embedding_and_frees_the_old_one(self):
        put(self.index, "t1", [line(0, gu="ભક્તિ"), line(1, gu="ભોજન")])
        self.fill()
        change = put(self.index, "t1", [line(0, gu="ભક્તિ"), line(1, gu="યાત્રા")])
        self.assertEqual((change.added, change.changed, change.removed, change.kept), (0, 1, 0, 1))
        self.assertEqual([text for _, text in self.index.missing_vectors()], ["યાત્રા"])
        self.assertEqual(self.fill(), 1)
        self.assertEqual(self.index.stats()["vectors"], 2)
        self.assertEqual(put(self.index, "t1", [line(0, gu="ભક્તિ"), line(1, gu="યાત્રા")]).kept, 2)
        self.assertEqual(self.index.missing_vectors(), [])
        self.index.check()  # the word index still agrees with the passages it indexes

    def test_the_same_text_is_never_embedded_twice(self):
        put(self.index, "t1", [line(0, gu="ભક્તિ")])
        put(self.index, "t2", [line(0, gu="ભક્તિ")])
        self.assertEqual(self.fill(), 1)

    def test_a_rename_embeds_nothing_and_keeps_every_passage(self):
        put(self.index, "t1", [line(0, gu="ભક્તિ")], given="Old")
        self.fill()
        self.assertIs(self.index.rename("t1", {**NAMES, "given": "Bhakti sabha"}), True)
        self.assertIs(self.index.rename("t1", {**NAMES, "given": "Bhakti sabha"}), False)
        self.assertIsNone(self.index.rename("t9", NAMES))
        self.assertEqual(self.index.missing_vectors(), [])
        self.assertEqual(self.index.find("sabha")["talks"][0]["name"], "Bhakti sabha")
        self.assertEqual(self.index.find("old")["talks"], [])

    def test_dropping_a_talk_frees_its_vectors_for_reuse(self):
        put(self.index, "t1", [line(0, gu="ભક્તિ"), line(1, gu="ભોજન")])
        self.fill()
        self.assertTrue(self.index.drop_talk("t1"))
        self.assertFalse(self.index.drop_talk("t1"))
        self.assertEqual(self.index.stats()["vectors"], 0)
        self.assertEqual(self.index.find("bhakti")["talks"], [])
        put(self.index, "t2", [line(0, gu="યાત્રા")])
        self.fill()
        self.index.check()
        self.assertEqual((len(self.index.vectors), self.index.vectors.rows), (1, 2))  # a freed row was reused

    def test_context_for_a_question_caps_passages_per_talk(self):
        put(self.index, "t1", [line(n, gu=f"ભક્તિ {n}", en=f"Devotion {n}") for n in range(6)])
        put(self.index, "t2", [line(0, gu="ભક્તિ", en="Devotion")], given="Second")
        self.fill()
        context = self.index.context("devotion", vector=[1, 0, 0, 0], limit=10, per_talk=4)
        self.assertEqual(sorted(sum(1 for c in context if c["talk"] == t) for t in ("t1", "t2")), [1, 4])
        self.assertTrue(all(c["gu"] and c["en"] for c in context))


class VectorTableTest(unittest.TestCase):
    def test_nearest_rows_come_back_in_order_and_slots_are_reused(self):
        table = VectorTable(dims=3)
        a = table.add("a", [1, 0, 0])
        table.add("b", [0, 1, 0])
        table.add("c", [0.8, 0.6, 0])
        self.assertEqual([key for key, _ in table.nearest([1, 0.1, 0], 2)], ["a", "c"])
        table.remove("a")
        self.assertEqual([key for key, _ in table.nearest([1, 0, 0], 5)], ["c", "b"])
        self.assertEqual(table.add("d", [0, 0, 1]), a)

    def test_vectors_are_normalised(self):
        table = VectorTable(dims=2)
        table.add("a", [3, 4])
        self.assertAlmostEqual(table.nearest([3, 4], 1)[0][1], 1.0, places=2)


class SnapshotTest(unittest.TestCase):
    def test_a_saved_index_loads_with_its_words_and_vectors(self):
        index = TalkIndex(model="test-embed", dims=4)
        self.addCleanup(index.close)
        put(index, "t1", [line(0, gu="ભગવાનની ભક્તિ", gl="Bhagwan ni bhakti")], given="Bhakti")
        index.put_vectors({key: fake_vector(text) for key, text in index.missing_vectors()})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.sqlite"
            index.save(path)
            loaded = TalkIndex.load(path, model="test-embed", dims=4)
        self.addCleanup(loaded.close)
        self.assertEqual(loaded.talks(), {"t1": "s1"})
        self.assertEqual(len(hits(loaded.find("bhagvan"))), 1)
        self.assertEqual(hits(loaded.find("worship", vector=[1, 0, 0, 0]))[0]["why"], ["meaning"])
        self.assertEqual(loaded.missing_vectors(), [])

    def test_another_model_or_a_damaged_file_is_refused(self):
        index = TalkIndex(model="test-embed", dims=4)
        self.addCleanup(index.close)
        put(index, "t1", [line(0, gu="ભક્તિ")])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "index.sqlite"
            index.save(path)
            with self.assertRaises(ValueError):
                TalkIndex.load(path, model="other-embed", dims=4)
            path.write_bytes(b"not a database at all" * 100)
            with self.assertRaises(sqlite3.DatabaseError):
                TalkIndex.load(path, model="test-embed", dims=4)


class FakeResponse:
    def __init__(self, body):
        self.body = json.dumps(body).encode()

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class EmbedderTest(unittest.TestCase):
    def setUp(self):
        self.requests = []

        def urlopen(request, timeout):
            self.requests.append(request)
            count = len(json.loads(request.data)["requests"])
            return FakeResponse({"embeddings": [{"values": [3.0, 4.0]} for _ in range(count)]})

        patcher = mock.patch("urllib.request.urlopen", urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.embedder = gemini.Embedder(api_key="test-key")

    def test_documents_use_google_batch_request_and_document_prefix(self):
        vectors = self.embedder.documents(["ભક્તિ", "seva"])
        request = self.requests[0]
        self.assertEqual(
            request.full_url,
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents",
        )
        self.assertEqual(request.get_header("X-goog-api-key"), "test-key")
        self.assertNotIn("key=", request.full_url)
        body = json.loads(request.data)
        self.assertEqual(body["requests"][0], {
            "model": "models/gemini-embedding-2",
            "content": {"parts": [{"text": "title: none | text: ભક્તિ"}]},
            "output_dimensionality": 768,
        })  # fmt: skip
        self.assertEqual(vectors[0], [0.6, 0.8])

    def test_queries_use_the_search_or_question_prefix(self):
        self.embedder.query("seva")
        self.embedder.query("what is seva?", question=True)
        texts = [json.loads(r.data)["requests"][0]["content"]["parts"][0]["text"] for r in self.requests]
        self.assertEqual(
            texts, ["task: search result | query: seva", "task: question answering | query: what is seva?"]
        )

    def test_a_query_is_tried_once_even_when_told_to_slow_down(self):
        def busy(request, timeout):
            self.requests.append(request)
            body = io.BytesIO(b"slow down")
            self.addCleanup(body.close)
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "30"}, body)

        with mock.patch("urllib.request.urlopen", busy), mock.patch("time.sleep") as sleep:
            with self.assertRaises(LLMError):
                self.embedder.query("seva")
        self.assertEqual((len(self.requests), sleep.call_count), (1, 0))  # search falls back to words at once

    def test_large_batches_are_split(self):
        self.assertEqual(len(self.embedder.documents(["x"] * 250)), 250)
        self.assertEqual([len(json.loads(r.data)["requests"]) for r in self.requests], [100, 100, 50])


class FakeLLM:
    def __init__(self, replies, model):
        self.replies, self.model, self.calls = replies, model, []
        self.timeout = 300.0

    def complete(self, system, user, max_tokens=2048):
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Completion(reply)


SOURCES = [
    {"n": 1, "talk": "t1", "name": "Diwali", "time": "01:05", "gu": "સેવા કરો", "en": "Do seva", "sm": ""},
    {"n": 2, "talk": "t2", "name": "Holi", "time": "", "gu": "", "en": "", "sm": "Seva is bhakti"},
]


class AnswerTest(unittest.TestCase):
    def ask(self, *replies):
        queue, made = list(replies), []

        def make(model):
            made.append(FakeLLM(queue, model))
            return made[-1]

        return gemini.answer("What is seva?", SOURCES, make_llm=make), made

    def test_the_prompt_holds_only_the_numbered_sources_marked_as_data(self):
        reply = json.dumps({"answer": "Seva is service [1], a form of bhakti [2][7].", "sources": [1, 2, 7]})
        result, made = self.ask(reply)
        system, user = made[0].calls[0]
        self.assertIn("data", system.lower())
        self.assertIn("<sources>", user)
        self.assertIn('"n": 1', user)
        self.assertIn("સેવા કરો", user)
        self.assertEqual(made[0].model, "gemini-4-hydrogen")
        self.assertEqual(made[0].timeout, 40.0)
        # someone is waiting: one quick retry at most, even when Google says to slow down
        self.assertEqual((made[0].retries, made[0].rate_limit_retries, made[0].max_wait), (1, 1, 3.0))
        self.assertEqual(result["answer"], "Seva is service [1], a form of bhakti [2].")
        self.assertEqual(result["cited"], [1, 2])
        self.assertEqual(result["model"], "gemini-4-hydrogen")

    def test_only_whole_numbers_are_citations(self):
        result, _ = self.ask(json.dumps({"answer": "Seva is service.", "sources": [True, 1.0, "2", 2]}))
        self.assertEqual(result["cited"], [2])

    def test_an_unreadable_reply_raises(self):
        with self.assertRaises(LLMError):
            self.ask("I think seva means service.")
        with self.assertRaises(LLMError):
            self.ask(json.dumps({"answer": ""}))

    def test_a_refused_model_falls_back_once(self):
        refused = LLMError("HTTP 404", 404, "models/gemini-4-hydrogen is not found")
        result, made = self.ask(refused, json.dumps({"answer": "Service [1].", "sources": [1]}))
        self.assertEqual([llm.model for llm in made], ["gemini-4-hydrogen", "gemini-3.8-flash"])
        self.assertEqual(result["model"], "gemini-3.8-flash")

    def test_other_errors_do_not_fall_back(self):
        with self.assertRaises(LLMError):
            self.ask(LLMError("HTTP 500", 500, "server error"))


if __name__ == "__main__":
    unittest.main()
