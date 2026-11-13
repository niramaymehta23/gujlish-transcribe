"""Search across all talks through the site: word, spelling and meaning search, questions answered from the talks,
the moment a result opens at, and an index that follows every change one talk at a time, survives restarts through
one saved snapshot, and costs a hosted search no storage requests. Gemini is replaced by fakes throughout."""

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import web.server as web_server
from tests.helpers import DeferredExecutor, wait_for_background_check
from tests.test_web_catalogue import VISITOR, CatalogueBase
from tests.test_web_gpu_jobs import EXECUTION, FakeJobs
from transcriber.english.llm import Completion, LLMError
from web.search import service as search_service
from web.search.index import TalkIndex
from web.search.passages import Passage
from web.server import create_app
from web.settings import Settings

CONCEPTS = [("વાત", "talk", "conversation"), ("ભોજન", "food", "cooking"), ("વાર્તા", "story", "tale")]


def concept(text: str) -> list[float]:
    vector = [float(any(word in text for word in group)) for group in CONCEPTS]
    return vector + [0.0 if any(vector) else 1.0]


class FakeEmbedder:
    model, dims = "fake-embed", 4

    def __init__(self):
        self.seen, self.queries = [], []

    def documents(self, texts):
        self.seen.extend(texts)
        return [concept(text) for text in texts]

    def query(self, text, question=False):
        self.queries.append((text, question))
        return concept(text)


class FakeAnswers:
    """Stands in for the Gemini client: records each prompt and replies with the next reply."""

    def __init__(self, *replies):
        self.replies, self.prompts, self.models = list(replies), [], []

    def __call__(self, model):
        self.models.append(model)
        fake = self

        class Model:
            timeout = 300.0

            def complete(self, system, user, max_tokens=2048):
                fake.prompts.append((system, user))
                return Completion(fake.replies.pop(0))

        return Model()


SUMMARY = {
    "kind": "summary",
    "summary": {
        "title": "Seva ni vaat",
        "one_line": "A talk about service.",
        "overview": "Swamiji explains seva.",
        "themes": [{"theme": "Seva", "points": [{"point": "Serve without asking", "t": "00:05"}]}],
        "stories": [],
        "quotes": [],
        "asks": [],
    },
}


class SearchBase(CatalogueBase):
    def start(self, gpu_jobs=None, embedder=None, answers=None, **overrides):
        options = dict(hosted=True, secret_key="x" * 40, retention_hours=0, allow_gemini=True, gpu_hours_per_month=0)
        options.update(overrides)
        self.settings = Settings(**options)
        self.executor = DeferredExecutor()
        self.app = create_app(
            executor=self.executor, settings=self.settings, gpu_jobs=gpu_jobs, embedder=embedder, answer_llm=answers
        )
        self.app.config["SESSION_COOKIE_SECURE"] = False
        wait_for_background_check(self, self.app)
        self.search = self.app.extensions["asr_search"]
        self.search.embed_settle = 0  # embed at once rather than after the usual quiet spell
        self.client = self.browser(VISITOR)
        self.client.get("/health")  # the first request loads the talks and starts the search index
        self.idle()
        return self.app

    def idle(self, search=None):
        self.assertTrue((search or self.search).wait_idle(10), "the search index did not catch up")

    def find(self, query, client=None, **params):
        response = (client or self.client).get("/api/search", query_string={"q": query, **params})
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def talk_ids(self, result):
        return [talk["id"] for talk in result["talks"]]

    def job_id(self, run_dir):
        return next(entry["id"] for entry in self.history() if entry["run"] == run_dir.name)

    def write_summary(self, run_dir, summary=SUMMARY):
        self.write_output(run_dir, "summary")
        self.transcript(run_dir, ".summary.json").write_text(json.dumps(summary), encoding="utf-8")

    def puts(self):
        """Records which talks the index rewrites, and how."""
        real, calls = TalkIndex.put_talk, []

        def put_talk(index, talk, names, passages, stamp=""):
            change = real(index, talk, names, passages, stamp)
            calls.append((talk, change))
            return change

        patcher = patch.object(TalkIndex, "put_talk", put_talk)
        patcher.start()
        self.addCleanup(patcher.stop)
        return calls


class TestWordSearch(SearchBase):
    def test_a_talk_is_found_in_gujarati_gujlish_english_and_by_spelling(self):
        run_dir = self.add_talk(1, stages=("gujlish", "translation"))
        self.add_talk(2)
        self.start()
        job_id = self.job_id(run_dir)
        for query, column, at in (
            ("વાત", "gu", 0),
            ("vaat", "gl", 0),
            ("talk", "en", 0),
            ("vat", "gl", 0),
            ("nirmay", "gl", 5),
        ):
            with self.subTest(query):
                result = self.find(query)
                self.assertEqual(result["state"], "ready")
                found = next(talk for talk in result["talks"] if talk["id"] == job_id)
                self.assertEqual(found["hits"][0]["column"], column)
                # the line holding the word, not the start of its 40-second passage
                self.assertEqual(found["hits"][0]["link"], f"#talk={job_id}&t={at}")
                self.assertEqual(found["hits"][0]["time"], f"00:0{at}")
        self.assertEqual(self.find("talk")["talks"][0]["name"], "talk001.wav")

    def test_a_latin_query_finds_a_talk_without_gujlish(self):
        self.add_talk(1)
        self.start()
        hit = self.find("nirmay")["talks"][0]["hits"][0]
        self.assertEqual((hit["column"], hit["why"]), ("gu", ["spelling"]))

    def test_talk_names_and_summary_points_are_searched(self):
        run_dir = self.add_talk(1, stages=("gujlish",))
        self.write_summary(run_dir)
        self.start()
        found = self.find("seva")["talks"][0]
        self.assertTrue(found["name_hit"])
        self.assertEqual(found["title"], "Seva ni vaat")
        overview = next(hit for hit in found["hits"] if hit["kind"] == "summary")
        self.assertEqual(overview["link"], f"#talk={found['id']}&view=summary")
        point = self.find("asking")["talks"][0]["hits"][0]
        self.assertEqual((point["kind"], point["label"], point["time"]), ("point", "Seva", "00:05"))
        self.assertEqual(point["link"], f"#talk={found['id']}&t=5")

    def test_an_empty_or_long_query(self):
        self.add_talk(1)
        self.start()
        self.assertEqual(self.find("  ")["talks"], [])
        self.assertEqual(self.client.get("/api/search", query_string={"q": "x" * 501}).status_code, 400)

    def test_a_hosted_search_asks_nothing_of_the_storage(self):
        for number in range(3):
            self.add_talk(number, stages=("gujlish", "translation"))
        self.start(embedder=FakeEmbedder())
        self.find("vaat")  # warm
        with self.storage.counting() as trips:
            self.assertEqual(len(self.find("vaat")["talks"]), 3)
            self.assertEqual(len(self.find("conversation", meaning=1)["talks"]), 3)
            self.assertEqual(len(self.find("va", live=1)["talks"]), 3)
        self.assertEqual(trips.summary(), {"listing": 0, "read": 0, "write": 0})


class TestMeaningSearch(SearchBase):
    def test_a_query_sharing_no_words_finds_the_talk_by_meaning(self):
        self.add_talk(1, stages=("gujlish", "translation"))
        embedder = FakeEmbedder()
        self.start(embedder=embedder)
        self.assertEqual(embedder.seen, ["આજે વાત હા નીરમય\nToday a talk Yes Nirmay"])  # Gujarati and English only
        self.assertEqual(self.find("conversation")["talks"], [])  # words only unless asked
        result = self.find("conversation", meaning=1)
        self.assertEqual(result["meaning"], "on")
        self.assertEqual(result["talks"][0]["hits"][0]["why"], ["meaning"])
        self.assertEqual(embedder.queries, [("conversation", False)])
        self.find("conversation", meaning=1)
        self.assertEqual(len(embedder.queries), 1)  # the same query is not embedded again

    def test_without_gemini_words_still_answer(self):
        self.add_talk(1, stages=("gujlish",))
        self.start(allow_gemini=False)
        result = self.find("vaat", meaning=1)
        self.assertEqual(result["meaning"], "off")
        self.assertEqual(len(result["talks"]), 1)


class TestQuestions(SearchBase):
    def setUp(self):
        super().setUp()
        self.first = self.add_talk(1, stages=("gujlish", "translation"))
        self.other = self.add_talk(2, stages=("gujlish",))
        self.write_transcript(self.other, [{"start": 0, "end": 5, "text": "ભોજન બનાવો"}])

    def ask(self, question, status=200):
        response = self.client.post("/api/ask", json={"question": question})
        self.assertEqual(response.status_code, status, response.json)
        return response.json

    def test_an_answer_cites_the_talk_and_the_moment(self):
        answers = FakeAnswers(json.dumps({"answer": "It was a talk with Nirmay [1][4].", "sources": [1]}))
        self.start(embedder=FakeEmbedder(), answers=answers)
        reply = self.ask("What was the conversation about?")
        _, prompt = answers.prompts[0]
        self.assertIn("આજે વાત હા નીરમય", prompt)
        self.assertNotIn("ભોજન", prompt)  # only the passages retrieved for the question
        self.assertEqual(reply["answer"], "It was a talk with Nirmay [1].")
        job_id = self.job_id(self.first)
        self.assertEqual(reply["citations"], [{
            "n": 1, "id": job_id, "title": None, "name": "talk001.wav", "kind": "line", "label": "", "time": "00:00",
            "start": 0.0, "seg": 0, "link": f"#talk={job_id}&t=0",
        }])  # fmt: skip
        self.assertEqual(answers.models, ["gemini-4-hydrogen"])

    def test_nothing_found_asks_no_model(self):
        answers = FakeAnswers()
        self.start(embedder=FakeEmbedder(), answers=answers)
        reply = self.ask("qqqq zzzz?")
        self.assertEqual((reply["citations"], answers.models), ([], []))
        self.assertTrue(reply["answer"])

    def test_without_gemini_questions_are_refused(self):
        self.start(allow_gemini=False)
        self.assertIn("error", self.ask("What was the talk about?", status=400))

    def test_an_unreadable_answer_is_a_gateway_error(self):
        self.start(embedder=FakeEmbedder(), answers=FakeAnswers("I am not JSON"))
        self.assertIn("error", self.ask("What was the talk about?", status=502))

    def test_an_empty_question(self):
        self.start(embedder=FakeEmbedder(), answers=FakeAnswers())
        self.ask(" ", status=400)


class TestIncrementalUpdates(SearchBase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.add_talk(1, stages=("gujlish",), suggestion=True)
        self.untouched = self.add_talk(2, stages=("gujlish",))
        self.embedder = FakeEmbedder()
        self.start(embedder=self.embedder)
        self.job = self.job_id(self.run_dir)
        self.embedder.seen.clear()
        self.calls = self.puts()

    def test_a_word_fix_reindexes_and_reembeds_one_passage_of_one_talk(self):
        body = {"segment": 0, "start": 1, "end": 2, "heard": "વાત", "correct": "વાર્તા", "correct_gujlish": "vaarta"}
        self.assertEqual(
            self.client.post(f"/api/jobs/{self.job}/edit", json={**body, "remember": False}).status_code, 200
        )
        self.idle()
        self.assertEqual([(talk, change.changed, change.added) for talk, change in self.calls], [(self.job, 1, 0)])
        self.assertEqual(self.embedder.seen, ["આજે વાર્તા હા નીરમય"])
        self.assertEqual(self.talk_ids(self.find("vaarta")), [self.job])
        self.assertEqual(self.talk_ids(self.find("story", meaning=1)), [self.job])

    def test_a_dismissed_suggestion_changes_no_passage(self):
        body = {"segment": 1, "start": 1, "end": 2, "rule": "r1"}
        self.assertEqual(self.client.post(f"/api/jobs/{self.job}/dismiss", json=body).status_code, 200)
        self.idle()
        self.assertEqual([(talk, change.changed, change.kept) for talk, change in self.calls], [(self.job, 0, 1)])
        self.assertEqual(self.embedder.seen, [])

    def test_a_rename_rewrites_only_the_name(self):
        self.assertEqual(
            self.client.post(f"/api/jobs/{self.job}/rename", json={"title": "Diwali sabha"}).status_code, 200
        )
        self.idle()
        self.assertEqual((self.calls, self.embedder.seen), ([], []))
        found = self.find("diwali")["talks"]
        self.assertEqual(
            [(talk["id"], talk["title"], talk["name_hit"]) for talk in found], [(self.job, "Diwali sabha", True)]
        )

    def test_each_finished_stage_updates_that_talk(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "g"}):
            self.assertEqual(self.client.post(f"/api/jobs/{self.job}/translate").status_code, 202)
        self.run_task(lambda command: self.write_output(
            self.run_dir, "translation" if command[1].endswith("translate.py") else "captions"
        ))  # fmt: skip
        self.idle()
        self.assertEqual({talk for talk, _ in self.calls}, {self.job})
        self.assertEqual(self.embedder.seen, ["આજે વાત હા નીરમય\nToday a talk Yes Nirmay"])
        self.assertEqual(self.talk_ids(self.find("today")), [self.job])
        with patch.dict("os.environ", {"GEMINI_API_KEY": "g"}):
            self.assertEqual(self.client.post(f"/api/jobs/{self.job}/summarize").status_code, 202)
        self.run_task(lambda command: self.write_summary(self.run_dir))
        self.idle()
        found = self.find("seva")["talks"]
        self.assertEqual([(talk["id"], talk["title"]) for talk in found], [(self.job, "Seva ni vaat")])
        self.assertEqual({talk for talk, _ in self.calls}, {self.job})

    def test_a_new_talk_is_added_when_its_transcript_comes_in(self):
        run_dir = self.add_talk(3, finished=False)
        self.start(gpu_jobs=FakeJobs(), embedder=self.embedder)
        self.join_reconcile()  # the new folder is found by the check that follows a start from the catalogue
        self.idle()
        self.calls.clear()
        job_id = self.job_id(run_dir)
        function, args = self.executor.tasks.pop(0)

        def remote(jobs, command, on_event, on_start=None, **options):
            options["on_execution"](EXECUTION)
            self.write_transcript(run_dir, [{"start": 0, "end": 4, "text": "નવી વાર્તા"}])
            on_event(
                {
                    "stage": "complete",
                    "txt": str(self.transcript(run_dir, ".txt")),
                    "json": str(self.transcript(run_dir)),
                }
            )
            options["on_finish"](EXECUTION, {"started": time.time() - 60, "ended": time.time()})
            options["on_execution"](None)
            return 0, ["Done:"]

        with patch("web.server.run_remote", side_effect=remote):
            function(*args)
        self.idle()
        self.assertEqual([talk for talk, _ in self.calls], [job_id])
        self.assertEqual(self.talk_ids(self.find("નવી")), [job_id])

    def test_a_deleted_talk_leaves_the_index(self):
        self.assertEqual(self.client.post(f"/api/jobs/{self.job}/delete").status_code, 200)
        self.idle()
        self.assertNotIn(self.job, self.search.index.talks())
        self.assertEqual(self.talk_ids(self.find("vaat")), [self.job_id(self.untouched)])

    def test_a_delete_during_a_read_wins(self):
        reading, go_on = threading.Event(), threading.Event()
        real = search_service.read_talk

        def slow(*args, **kwargs):
            reading.set()
            go_on.wait(5)
            return real(*args, **kwargs)

        with patch("web.search.service.read_talk", side_effect=slow):
            self.search.touch(self.job)
            self.assertTrue(reading.wait(5))
            self.assertEqual(self.client.post(f"/api/jobs/{self.job}/delete").status_code, 200)
            go_on.set()
            self.idle()
        self.assertNotIn(self.job, self.search.index.talks())
        self.assertEqual(self.talk_ids(self.find("vaat")), [self.job_id(self.untouched)])

    def test_talks_past_the_retention_period_leave_the_index(self):
        old = self.add_talk(4, run="20200101-120000-000004")
        self.start(retention_hours=24, embedder=self.embedder)
        self.join_reconcile()
        self.idle()
        old_id = next(job for job, value in self.app.extensions["asr_jobs"].items() if value["run_dir"] == str(old))
        self.assertIn(old_id, self.search.index.talks())
        self.history()  # the list sweeps first
        self.idle()
        self.assertNotIn(old_id, self.search.index.talks())


class TestSnapshots(SearchBase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.add_talk(1, stages=("gujlish", "translation"))
        self.add_talk(2, stages=("gujlish",))
        self.embedder = FakeEmbedder()
        self.start(embedder=self.embedder)
        self.search.flush()
        self.snapshot = self.root / ".search" / "index.sqlite"

    def restart(self):
        self.embedder.seen.clear()
        reads = []
        real = search_service.read_talk
        with patch("web.search.service.read_talk", side_effect=lambda *a, **k: reads.append(a) or real(*a, **k)):
            self.start(embedder=self.embedder)
        return reads

    def test_a_restart_reads_no_talk_and_embeds_nothing(self):
        self.assertTrue(self.snapshot.is_file())
        self.assertFalse(list(self.snapshot.parent.glob("*.tmp")))  # written under a temporary name, then renamed
        self.assertEqual(self.restart(), [])
        self.assertEqual(self.embedder.seen, [])
        self.assertEqual(len(self.find("vaat")["talks"]), 2)
        self.assertEqual(len(self.find("conversation", meaning=1)["talks"]), 2)

    def test_a_change_after_the_snapshot_is_found_after_a_restart(self):
        job_id = self.job_id(self.run_dir)
        body = {"segment": 0, "start": 1, "end": 2, "heard": "વાત", "correct": "વાર્તા", "remember": False}
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/edit", json=body).status_code, 200)
        self.idle()  # not saved yet: the next save waits a minute
        reads = self.restart()
        self.assertEqual([Path(args[0]).parts[-4] for args in reads], ["talk001"])
        self.assertEqual(self.talk_ids(self.find("વાર્તા")), [job_id])

    def test_a_damaged_snapshot_is_rebuilt(self):
        self.snapshot.write_bytes(b"\0" * 4096)
        self.assertEqual(len(self.restart()), 2)
        self.assertEqual(len(self.find("vaat")["talks"]), 2)
        self.assertEqual(len(self.embedder.seen), 2)

    def test_a_snapshot_never_creates_the_storage_folder(self):
        self.search.folder = self.root / "gone" / ".search"
        self.search.touch(self.job_id(self.run_dir))
        self.idle()
        self.search.flush()  # quietly does nothing: the storage is not there
        self.assertFalse((self.root / "gone").exists())


class TestStartUp(SearchBase):
    def test_search_starts_even_when_resuming_cut_short_work_failed(self):
        self.add_talk(1)
        real, failed = web_server.read_meta, []

        def flaky(run_dir):
            if not failed:
                failed.append(run_dir)
                raise OSError("the storage did not answer")
            return real(run_dir)

        with patch("web.server.read_meta", side_effect=flaky), patch("flask.Flask.log_exception"):
            self.start(hosted=False)  # its first request fails
        self.assertTrue(failed)
        self.assertEqual(self.find("vaat")["state"], "ready")


class TestLocalSite(SearchBase):
    def test_a_change_from_the_command_line_is_found(self):
        run_dir = self.add_talk(1)
        self.start(hosted=False)
        self.search.check_every = 0
        self.assertEqual(len(self.find("vaat")["talks"]), 1)
        self.write_transcript(run_dir, [{"start": 0, "end": 5, "text": "નવો શબ્દ"}])
        self.assertEqual(len(self.find("નવો")["talks"]), 1)
        self.assertEqual(self.find("vaat")["talks"], [])


class TestMoments(SearchBase):
    def test_a_moment_plays_from_the_recording(self):
        self.add_talk(1)
        self.start()
        job_id = self.history()[0]["id"]
        cuts = []
        with patch("web.server.clip_audio", side_effect=lambda path, start, end: cuts.append((start, end)) or b"RIFF"):
            response = self.client.get(f"/api/jobs/{job_id}/moment", query_string={"start": 2, "end": 100})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.mimetype, "audio/wav")
            self.assertEqual(self.client.get(f"/api/jobs/{job_id}/moment", query_string={"start": 3}).status_code, 200)
        self.assertEqual(cuts, [(2.0, 10.0), (3.0, 10.0)])  # never past the end of the talk, at most 45 seconds
        for bad in ({"start": -1}, {"start": "x"}, {"start": 5, "end": 4}, {"start": "nan"}, {}):
            self.assertEqual(self.client.get(f"/api/jobs/{job_id}/moment", query_string=bad).status_code, 400)

    def test_a_moment_is_capped_and_needs_a_finished_talk(self):
        run_dir = self.add_talk(1)
        data = json.loads(self.transcript(run_dir).read_text(encoding="utf-8"))
        self.transcript(run_dir).write_text(json.dumps({**data, "duration": 600.0}), encoding="utf-8")
        self.add_talk(2, finished=False)
        self.start()
        finished, unfinished = (entry["id"] for entry in sorted(self.history(), key=lambda entry: entry["name"]))
        cuts = []
        with patch("web.server.clip_audio", side_effect=lambda path, start, end: cuts.append((start, end)) or b"RIFF"):
            self.client.get(f"/api/jobs/{finished}/moment", query_string={"start": 100, "end": 400})
            self.assertEqual(
                self.client.get(f"/api/jobs/{unfinished}/moment", query_string={"start": 1}).status_code, 404
            )
        self.assertEqual(cuts, [(100.0, 145.0)])


class SampleTalks:
    """Talks for the search service on its own, in memory."""

    def __init__(self):
        self.text = {"t1": "Seva is service", "t2": "Bhakti is devotion"}
        self.given = {}
        self.reads = []

    def talks(self):
        return {talk: self.names(talk) for talk in self.text}

    def names(self, talk):
        return {"given": self.given.get(talk, ""), "auto": "", "file": f"{talk}.mp3"} if talk in self.text else None

    def read(self, talk):
        self.reads.append(talk)
        if talk not in self.text:
            return None
        passage = Passage("line", 0, 0.0, 5.0, 0, gu="વાત", en=self.text[talk])
        return search_service.TalkText(self.stamp(talk), self.names(talk), [passage])

    def stamp(self, talk):
        return f"s:{self.text[talk]}" if talk in self.text else None


class Embeddings:
    """An embedder whose replies the test chooses; counts its requests."""

    model, dims = "fake-embed", 4

    def __init__(self, reply=None, delay=0.0):
        self.calls, self.reply, self.delay, self.started = 0, reply, delay, threading.Event()

    def documents(self, texts):
        self.calls += 1
        self.started.set()
        time.sleep(self.delay)
        if isinstance(self.reply, Exception):
            raise self.reply
        return [self.reply or concept(text) for text in texts]

    def query(self, text, question=False):
        return concept(text)


class TestWorker(unittest.TestCase):
    """The worker's timing: saves, retries and waits, with the service on its own (no site)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name) / ".search"
        self.talks = SampleTalks()

    def service(self, embedder=None, trusted=True):
        service = search_service.SearchService(
            folder=self.folder, talks=self.talks.talks, names=self.talks.names, read=self.talks.read,
            stamp=self.talks.stamp, embedder=embedder, meaning=lambda: embedder is not None, trusted=trusted,
        )  # fmt: skip
        service.embed_settle = 0
        self.addCleanup(self.stop, service)
        return service

    def stop(self, service):
        with service.cond:
            service.pending.clear()
            service.checks.clear()
            service.dirty_since, service.unembedded = None, False
            service.cond.notify_all()

    def until(self, check, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not check() and time.monotonic() < deadline:
            time.sleep(0.02)
        return check()

    def test_the_index_is_saved_while_gemini_keeps_refusing_embeddings(self):
        embedder = Embeddings(reply=LLMError("HTTP 429: quota"))
        with patch.object(search_service, "SAVE_AFTER", 0.05), patch.object(search_service, "SAVE_AT_MOST", 0.1):
            with patch("builtins.print"):
                self.service(embedder).start()
                self.assertTrue(self.until(lambda: (self.folder / "index.sqlite").is_file()))
        self.assertGreaterEqual(embedder.calls, 1)

    def test_a_failed_save_waits_a_minute_before_trying_again(self):
        service, saves = self.service(), []
        real = service._save
        service._save = lambda: saves.append(1) or real()
        with patch.object(search_service, "SAVE_AFTER", 0.0), patch.object(search_service, "SAVE_AT_MOST", 0.0):
            with patch("web.search.service.os.replace", side_effect=PermissionError("held")), patch("builtins.print"):
                service.start()
                self.assertTrue(service.wait_idle(3))
                time.sleep(0.5)
        self.assertEqual(saves, [1])
        self.assertGreater(service.save_after, time.monotonic() + 50)

    def test_a_snapshot_the_storage_failed_to_read_is_read_again_not_rebuilt(self):
        first = self.service()
        first.start()
        self.assertTrue(first.wait_idle(3))
        first.flush()
        self.talks.reads.clear()
        real, failures = shutil.copyfile, []

        def flaky(source, target, *args, **kwargs):
            if not failures:
                failures.append(source)
                raise OSError(5, "Input/output error")
            return real(source, target, *args, **kwargs)

        with (
            patch.object(search_service, "LOAD_RETRY", (0.05, 0.05)),
            patch("web.search.service.shutil.copyfile", flaky),
        ):
            with patch("builtins.print"):
                again = self.service()
                again.start()
                self.assertTrue(again.wait_idle(3))
        self.assertEqual(len(failures), 1)
        self.assertEqual(self.talks.reads, [])  # nothing re-read, so nothing re-embedded or overwritten
        self.assertEqual(set(again.index.talks()), {"t1", "t2"})

    def test_vectors_of_the_wrong_size_are_not_asked_for_again_at_once(self):
        embedder = Embeddings(reply=[1.0, 0.0, 0.0])  # three numbers where four were asked for
        with patch("builtins.print"):
            self.service(embedder).start()
            self.assertTrue(self.until(lambda: embedder.calls >= 1))
            time.sleep(0.5)
        self.assertEqual(embedder.calls, 1)  # waits before paying for the same request again

    def test_a_local_search_waits_only_for_changes_it_found(self):
        embedder = Embeddings(delay=1.5)
        service = self.service(embedder, trusted=False)
        service.check_every = 0
        service.start()
        self.assertTrue(service.wait_idle(3, vectors=False))
        self.assertTrue(embedder.started.wait(3))  # Gemini is busy embedding
        began = time.monotonic()
        service.search("seva")
        self.assertLess(time.monotonic() - began, 0.5)

    def test_hooks_never_wait_for_a_talk_being_written(self):
        service, writing = self.service(), threading.Event()
        service.start()
        self.assertTrue(service.wait_idle(3))
        real = TalkIndex.put_talk

        def slow(index, *args, **kwargs):
            writing.set()
            time.sleep(1.0)
            return real(index, *args, **kwargs)

        self.talks.text["t1"] = "Seva is selfless service"
        with patch.object(TalkIndex, "put_talk", slow):
            service.touch("t1")
            self.assertTrue(writing.wait(3))
            began = time.monotonic()
            service.rename("t2")  # a server hook, called under the server's locks
            self.assertLess(time.monotonic() - began, 0.2)
            self.assertTrue(service.wait_idle(3))

    def test_a_rename_never_undoes_a_newer_change(self):
        service = self.service()  # not started: nothing runs on its own
        generation = service.generations.get("t3", 0)
        service.touch("t3")  # its transcript arrived after the rename was picked up
        service._process("t3", "rename", generation)  # t3 has no names yet (unfinished)
        self.assertEqual(service.pending, {"t3": "touch"})

    def test_temporary_files_left_by_an_interrupted_save_are_removed(self):
        self.folder.mkdir()
        old, recent = self.folder / ".index.sqlite.dead.tmp", self.folder / ".index.sqlite.live.tmp"
        for path in (old, recent):
            path.write_bytes(b"part of a save")
        hours_ago = time.time() - 7200
        os.utime(old, (hours_ago, hours_ago))
        service = self.service()
        service.start()
        self.assertTrue(service.wait_idle(3))
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())  # may be another server's save in progress

    def test_a_save_that_keeps_failing_waits_longer_each_time(self):
        service = self.service()
        with patch.object(search_service, "SAVE_AFTER", 0.0), patch.object(search_service, "SAVE_AT_MOST", 0.0):
            with patch("web.search.service.os.replace", side_effect=PermissionError("held")), patch("builtins.print"):
                service.start()
                self.assertTrue(self.until(lambda: service.save_after > 0))
                waits = []
                for _ in range(6):
                    service._save()
                    waits.append(round(service.save_after - time.monotonic()))
        self.assertEqual(waits, [120, 240, 480, 960, 1800, 1800])  # a full write at most every half hour
        service._save()  # the storage answers again
        self.assertEqual((service.save_after, service.save_wait), (0.0, 60.0))

    def test_a_snapshot_with_damaged_embeddings_is_rebuilt_once(self):
        first = self.service(Embeddings())
        first.start()
        self.assertTrue(first.wait_idle(3))
        first.flush()
        db = sqlite3.connect(self.folder / "index.sqlite")
        with db:
            self.assertGreater(db.execute("SELECT COUNT(*) FROM vectors").fetchone()[0], 0)
            db.execute("UPDATE vectors SET value = 'x'")
        db.close()
        real, reads = shutil.copyfile, []

        def counted(source, target, *args, **kwargs):
            if Path(source).name == "index.sqlite":
                reads.append(source)
            return real(source, target, *args, **kwargs)

        with patch("web.search.service.shutil.copyfile", counted), patch("builtins.print"):
            again = self.service(Embeddings())
            again.start()
            self.assertTrue(again.wait_idle(3))
            time.sleep(0.3)
        self.assertEqual(len(reads), 1)  # read once and rebuilt, never read again in a loop
        self.assertEqual(set(again.index.talks()), {"t1", "t2"})

    def test_an_unexpected_fault_while_loading_waits_before_trying_again(self):
        service, tries = self.service(), []

        def broken():
            tries.append(1)
            raise TypeError("unexpected")

        service._restore = broken
        with patch("builtins.print"):
            service.start()
            time.sleep(0.3)
        self.assertEqual(len(tries), 1)
        self.assertGreater(service.load_after, time.monotonic())
        with service.cond:
            service.load_after = time.monotonic() + 3600  # the test is over: no further tries

    def test_hooks_never_wait_for_the_check_after_embedding(self):
        service, checking = self.service(Embeddings()), threading.Event()
        real = TalkIndex.missing_vectors

        def slow(index, limit=100):
            if limit == 1:  # is any text left to embed?
                checking.set()
                time.sleep(1.0)
            return real(index, limit)

        with patch.object(TalkIndex, "missing_vectors", slow):
            service.start()
            self.assertTrue(checking.wait(3))
            began = time.monotonic()
            service.rename("t2")  # a server hook, called under the server's locks
            self.assertLess(time.monotonic() - began, 0.2)
            self.assertTrue(service.wait_idle(5))

    def test_a_local_check_never_undoes_a_newer_rename(self):
        service = self.service(trusted=False)
        service.check_every = 0
        service.start()
        self.assertTrue(service.wait_idle(3))
        before = self.talks.talks()  # the check's list of talks, taken just before the rename below
        self.talks.given["t1"] = "Seva talk"
        service.rename("t1")
        self.assertTrue(service.wait_idle(3))
        service._talks = lambda: before
        service.search("seva")
        self.assertTrue(service.wait_idle(3))
        self.assertEqual(service.index.names()["t1"][0], "Seva talk")
        # A rename made from the command line is still found, and indexed by the worker.
        service._talks = self.talks.talks
        self.talks.given["t2"] = "Bhakti talk"
        service.search("bhakti")
        self.assertTrue(service.wait_idle(3))
        self.assertEqual(service.index.names()["t2"][0], "Bhakti talk")
        self.assertIsNotNone(service.dirty_since)  # so it is saved
