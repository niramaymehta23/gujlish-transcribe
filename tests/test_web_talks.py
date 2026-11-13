"""Talks shared by everyone signed in (the site password is the account), their names, and signing out.

Every signed-in browser lists and opens every talk. A talk's name is the one you gave it, else the title its summary
gave it, else its file name; a name you gave always wins, and every device sees it at once."""

import json
import unicodedata
from pathlib import Path
from threading import Event, Thread
from unittest.mock import patch

import web.server as server
from tests.test_hosted import HostedBase
from tests.test_web_catalogue import ALL_STAGES, STRANGER, CatalogueBase
from transcriber.english.summarize import UNTITLED
from web.server import CATALOGUE_VERSION, RUN_FILE, TITLE_LIMIT

PASSWORD = "open sesame"
NOTHING = {"listing": 0, "read": 0, "write": 0}


def held_once(function, name: str):
    """`function` made to wait, the first time it is called with a path named `name`, until `go_on` is set: so a
    test can act while a request is part-way. Returns the stand-in, an Event set when it is waiting, and `go_on`."""
    waiting, go_on = Event(), Event()

    def stand_in(path, *args, **kwargs):
        if Path(path).name == name and not waiting.is_set():
            waiting.set()
            go_on.wait(5)
        return function(path, *args, **kwargs)

    return stand_in, waiting, go_on


class TestOneAccount(HostedBase):
    def signed_in(self, app):
        client = app.test_client()
        self.assertEqual(client.post("/login", data={"password": PASSWORD}).status_code, 302)
        return client

    def finish(self, app, job_id):
        """Write the transcript the transcriber would, and mark the run finished."""
        job = app.extensions["asr_jobs"][job_id]
        txt = Path(job["run_dir"]) / "talk.flex.txt"
        txt.write_text("આજે machine learning\n", encoding="utf-8")
        data = {"source": "talk.wav", "model": "flex", "mode": "mixed", "duration": 12, "segments": []}
        txt.with_suffix(".json").write_text(json.dumps(data), encoding="utf-8")
        job.update(status="complete", txt=str(txt), json=str(txt.with_suffix(".json")), done=())

    def test_a_talk_started_in_one_browser_is_listed_and_opens_in_another(self):
        app = self.app(password=PASSWORD)
        phone, laptop = self.signed_in(app), self.signed_in(app)
        job_id = self.start_job(phone).json["id"]
        self.assertFalse(Path(app.extensions["asr_jobs"][job_id]["run_dir"], ".owner").exists())
        self.assertEqual([run["id"] for run in laptop.get("/api/history").json], [job_id])
        self.finish(app, job_id)
        opened = laptop.get(f"/api/jobs/{job_id}")
        self.assertEqual((opened.status_code, opened.json["text"]), (200, "આજે machine learning\n"))
        self.assertNotIn("owner", opened.json)
        download = laptop.get(f"/api/jobs/{job_id}/download/txt")
        self.assertEqual(download.status_code, 200)
        download.close()
        sha = app.extensions["asr_jobs"][job_id]["sha256"]
        self.assertEqual([run["id"] for run in laptop.get(f"/api/recordings/{sha}").json["runs"]], [job_id])
        self.assertEqual(laptop.post(f"/api/jobs/{job_id}/delete").status_code, 200)
        self.assertEqual(phone.get("/api/history").json, [])

    def test_a_half_sent_upload_stays_with_its_browser(self):
        app = self.app(password=PASSWORD)
        phone, laptop = self.signed_in(app), self.signed_in(app)
        upload_id = phone.post("/api/uploads").json["id"]
        self.assertEqual(laptop.put(f"/api/uploads/{upload_id}?offset=0", data=b"a").status_code, 404)
        self.assertEqual(phone.put(f"/api/uploads/{upload_id}?offset=0", data=b"a").status_code, 200)

    def test_one_recording_transcribes_at_a_time_for_the_whole_site(self):
        app = self.app(password=PASSWORD)
        phone, laptop = self.signed_in(app), self.signed_in(app)
        sent = self.upload(laptop, self.source.read_bytes())  # sent while the site was free
        part = list((self.root / "tmp" / "vaani-uploads").glob(f"*-{sent}.part"))
        self.assertEqual(len(part), 1)
        first = self.start_job(phone).json["id"]
        busy = laptop.post("/api/uploads")  # told before sending the file, not after
        self.assertEqual(busy.status_code, 429)
        self.assertIn("already being transcribed", busy.json["error"])
        refused = laptop.post("/api/jobs", data={"upload_id": sent, "name": "talk.wav", "model": "flex"})
        self.assertEqual(refused.status_code, 429)
        self.assertFalse(part[0].exists())  # a refused upload is not kept
        self.assertEqual(phone.post(f"/api/jobs/{first}/pause").status_code, 200)  # waiting, so paused at once
        second = self.start_job(laptop)
        self.assertEqual(second.status_code, 202, second.json)
        resumed = phone.post(f"/api/jobs/{first}/resume")
        self.assertEqual(resumed.status_code, 429)
        self.assertEqual(app.extensions["asr_jobs"][first]["status"], "paused")
        self.assertEqual(laptop.post(f"/api/jobs/{second.json['id']}/pause").status_code, 200)
        self.assertEqual(phone.post(f"/api/jobs/{first}/resume").status_code, 202)

    def test_two_starts_at_once_are_never_both_let_through(self):
        app = self.app(password=PASSWORD)
        phone, laptop = self.signed_in(app), self.signed_in(app)
        audio = self.source.read_bytes()
        sent = {"phone": self.upload(phone, audio), "laptop": self.upload(laptop, audio)}
        probe, probing, go_on = held_once(server.probe_audio, self.source.name)
        started = {}

        def start(device, client):
            form = {"upload_id": sent[device], "name": self.source.name, "model": "flex"}
            started[device] = client.post("/api/jobs", data=form)

        with patch("web.server.probe_audio", side_effect=probe):
            phone_start = Thread(target=start, args=("phone", phone))
            phone_start.start()
            self.assertTrue(probing.wait(5))  # the phone's start is let through and checking its audio
            start("laptop", laptop)
            go_on.set()
            phone_start.join(5)
        self.assertEqual((started["phone"].status_code, started["laptop"].status_code), (202, 429))
        self.assertEqual(len(app.extensions["asr_jobs"]), 1)
        self.assertEqual(laptop.post("/api/uploads").status_code, 429)  # the place is the queued run's now

    def test_a_failed_recording_does_not_hold_the_site(self):
        app = self.app(password=PASSWORD)
        client = self.signed_in(app)
        first = self.start_job(client).json["id"]
        app.extensions["asr_jobs"][first].update(status="error", message="Transcription stopped.")
        self.assertEqual(self.start_job(client).status_code, 202)

    def test_a_local_site_has_no_limit(self):
        client = self.app(hosted=False).test_client()
        self.assertEqual(self.start_job(client).status_code, 202)
        self.assertEqual(self.start_job(client).status_code, 202)

    def test_signing_out_needs_the_password_again(self):
        app = self.app(password=PASSWORD)
        client = self.signed_in(app)
        job_id = self.start_job(client).json["id"]
        self.assertIn("Sign out", client.get("/").get_data(as_text=True))
        signed_out = client.post("/logout")
        self.assertEqual((signed_out.status_code, signed_out.location), (302, "/login"))
        self.assertEqual(client.get("/").location, "/login")
        for path in ("/api/history", f"/api/jobs/{job_id}", "/api/capabilities"):
            self.assertEqual(client.get(path).status_code, 401, path)
        self.assertEqual(client.post(f"/api/jobs/{job_id}/rename", json={"title": "x"}).status_code, 401)
        self.assertEqual(client.post("/login", data={"password": PASSWORD}).status_code, 302)
        self.assertEqual(client.get(f"/api/jobs/{job_id}").status_code, 200)

    def test_sign_out_shows_only_with_a_password(self):
        for hosted in (True, False):
            with self.subTest(hosted=hosted):
                client = self.app(hosted=hosted).test_client()
                self.assertNotIn("Sign out", client.get("/").get_data(as_text=True))
                self.assertEqual(client.post("/logout").status_code, 302)  # harmless: back to the page
                self.assertEqual(client.get("/").status_code, 200)
        local = self.app(hosted=False, password=PASSWORD).test_client()
        self.assertEqual(local.post("/login", data={"password": PASSWORD}).status_code, 302)
        self.assertIn("Sign out", local.get("/").get_data(as_text=True))


class TestNames(CatalogueBase):
    def rename(self, job_id, title, status=200, client=None):
        response = (client or self.client).post(f"/api/jobs/{job_id}/rename", json={"title": title})
        self.assertEqual(response.status_code, status, response.json)
        return response.json

    def naming(self, client=None) -> tuple:
        entry = self.history(client)[0]
        opened = self.open(entry["id"], client)
        self.assertEqual((opened["title"], opened["title_from"]), (entry["title"], entry["title_from"]))
        self.assertEqual(opened["name"], entry["name"])  # the file name, always there
        return entry["title"], entry["title_from"]

    def meta(self, run_dir) -> dict:
        return json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))

    def write_summary(self, run_dir, title):
        """What the summary script writes, its summary titled `title`."""
        self.write_output(run_dir, "summary")
        summary = {"kind": "summary", "summary": {"title": title, "overview": "About seva."}}
        self.transcript(run_dir, ".summary.json").write_text(json.dumps(summary), encoding="utf-8")

    def summarize(self, job_id, run_dir, title):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "g"}):
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/summarize").status_code, 202)
        self.run_task(lambda command: self.write_summary(run_dir, title))
        self.assertEqual(self.open(job_id)["summary"]["status"], "complete")

    def test_a_rename_shows_at_once_everywhere(self):
        run_dir = self.add_talk(1, stages=ALL_STAGES)
        self.start()
        job_id = self.history()[0]["id"]
        before = self.open(job_id)  # its files are cached now
        self.assertEqual((before["title"], before["title_from"]), (None, None))
        with self.storage.counting() as trips:
            named = self.rename(job_id, "  Kindness   at work ")
        self.assertEqual(named, {"title": "Kindness at work", "title_from": "user"})
        self.assertEqual(trips.count("listing"), 0)
        with self.storage.counting() as trips:
            listed, opened = self.history(), self.open(job_id)
        self.assertEqual(trips.summary(), NOTHING)  # from memory: no folder scan, no file read
        self.assertEqual(
            (listed[0]["title"], listed[0]["title_from"], listed[0]["name"]), (*named.values(), "talk001.wav")
        )
        self.assertEqual(opened, {**before, **named})
        self.assertEqual(self.naming(self.browser(STRANGER)), ("Kindness at work", "user"))
        self.assertEqual(self.meta(run_dir)["title"], "Kindness at work")
        self.assertEqual(
            (self.saved_runs()[0]["title"], self.saved_runs()[0]["auto_title"]), ("Kindness at work", None)
        )

    def test_a_rename_survives_restarts(self):
        run_dir = self.add_talk(1)
        self.start()
        job_id = self.history()[0]["id"]
        self.rename(job_id, "Satsang, 4 April")
        for restart in ("from the catalogue", "rebuilt from the folders", "on a local site"):
            with self.subTest(restart):
                if restart == "rebuilt from the folders":
                    self.catalogue.unlink()
                self.start(hosted=restart != "on a local site")
                self.history()
                self.join_reconcile()
                self.assertEqual(self.naming(), ("Satsang, 4 April", "user"))
        self.assertEqual(self.rename(job_id, ""), {"title": None, "title_from": None})  # back to the file name
        self.start(hosted=False)
        self.assertEqual(self.naming(), (None, None))
        self.assertNotIn("title", self.meta(run_dir))

    def test_the_background_check_takes_a_rename_the_catalogue_missed(self):
        run_dir = self.add_talk(1)
        self.start()
        self.history()
        # Renamed by another version of the site running alongside during a deploy, or a save cut short.
        (run_dir / RUN_FILE).write_text(json.dumps({**self.meta(run_dir), "title": "Named elsewhere"}), "utf-8")
        self.start()
        self.history()
        self.join_reconcile()
        self.assertEqual(self.naming(), ("Named elsewhere", "user"))
        self.assertEqual(self.saved_runs()[0]["title"], "Named elsewhere")

    def test_a_finished_summary_names_the_talk_unless_renamed(self):
        run_dir = self.add_talk(1, stages=("gujlish", "translation", "captions"))
        self.start()
        job_id = self.history()[0]["id"]
        self.summarize(job_id, run_dir, " Seva in  daily life ")
        self.assertEqual(self.naming(), ("Seva in daily life", "summary"))
        self.assertEqual(self.saved_runs()[0]["auto_title"], "Seva in daily life")
        self.assertNotIn("title", self.meta(run_dir))  # a name from the summary is never stored as yours
        self.rename(job_id, "Satsang, 4 April")
        self.summarize(job_id, run_dir, "Another summary title")
        self.assertEqual(self.naming(), ("Satsang, 4 April", "user"))
        for restart in ("from the catalogue", "rebuilt from the folders"):
            with self.subTest(restart):
                if restart == "rebuilt from the folders":
                    self.catalogue.unlink()
                self.start()
                self.history()
                self.join_reconcile()
                self.assertEqual(self.naming(), ("Satsang, 4 April", "user"))
        self.assertEqual(self.rename(job_id, ""), {"title": "Another summary title", "title_from": "summary"})
        self.summarize(job_id, run_dir, UNTITLED)  # the model gave no title this time
        self.assertEqual(self.naming(), (None, None))

    def test_a_summary_named_even_when_the_youtube_details_after_it_failed(self):
        run_dir = self.add_talk(1, stages=("gujlish", "translation", "captions"))
        self.start()
        job_id = self.history()[0]["id"]
        with patch.dict("os.environ", {"GEMINI_API_KEY": "g"}):
            self.assertEqual(self.client.post(f"/api/jobs/{job_id}/summarize").status_code, 202)
        function, args = self.executor.tasks.pop(0)

        def worker(command, on_event, on_start=None):
            self.write_summary(run_dir, "Seva in daily life")  # written before the YouTube details
            return 1, ["Error: the YouTube details could not be written."]

        with patch("web.server.run_worker", side_effect=worker):
            function(*args)
        self.assertEqual(self.open(job_id)["summary"]["status"], "error")
        self.assertEqual(self.naming(), ("Seva in daily life", "summary"))
        self.start()  # and the same after a restart
        self.assertEqual(self.naming(), ("Seva in daily life", "summary"))

    def test_talks_summarised_before_get_named_on_the_next_start(self):
        named, untitled = self.add_talk(1, stages=ALL_STAGES), self.add_talk(2, stages=ALL_STAGES)
        self.write_summary(named, "Seva in daily life")
        self.write_summary(untitled, UNTITLED)
        self.add_talk(3)
        expected = {"talk001.wav": "Seva in daily life", "talk002.wav": None, "talk003.wav": None}

        def titles():
            return {entry["name"]: entry["title"] for entry in self.history()}

        self.start()  # no catalogue yet: built from the folders
        self.assertEqual(titles(), expected)
        # A catalogue saved by the version before names, which kept none.
        old = {"version": 1, "runs": [{**run, "title": None} for run in self.saved_runs()]}
        for run in old["runs"]:
            run.pop("auto_title")
        self.catalogue.write_text(json.dumps(old), encoding="utf-8")
        with patch("builtins.print"):
            self.start()
            self.assertEqual(titles(), expected)
        self.assertEqual(json.loads(self.catalogue.read_text(encoding="utf-8"))["version"], CATALOGUE_VERSION)
        self.start(hosted=False)
        self.assertEqual(titles(), expected)

    def test_rename_input(self):
        self.add_talk(1)
        self.start()
        job_id = self.history()[0]["id"]
        decomposed = unicodedata.normalize("NFD", "Café")
        self.assertEqual(self.rename(job_id, f"{decomposed}\x07talk\n")["title"], "Café talk")
        self.assertEqual(self.rename(job_id, "શ્‌રી ગીતા")["title"], "શ્‌રી ગીતા")  # joiners kept
        self.assertEqual(self.rename(job_id, "\u202eevil\u202c name")["title"], "evil name")  # no direction overrides
        self.assertEqual(self.rename(job_id, "A\ud800B")["title"], "A B")  # a lone surrogate can't be saved
        self.assertEqual(self.rename(job_id, "x" * TITLE_LIMIT)["title"], "x" * TITLE_LIMIT)
        self.assertIn("characters", self.rename(job_id, "x" * (TITLE_LIMIT + 1), 400)["error"])
        for body in ({"title": 5}, {}, None, ["x"], "x"):
            response = self.client.post(f"/api/jobs/{job_id}/rename", json=body)
            self.assertEqual(response.status_code, 400, body)
        self.assertEqual(self.history()[0]["title"], "x" * TITLE_LIMIT)
        for blank in ("\u200b", " \u200d ", "\u0301"):  # nothing that shows: the automatic name comes back
            self.rename(job_id, "Named")
            self.assertEqual(self.rename(job_id, blank), {"title": None, "title_from": None}, repr(blank))
        self.rename("0" * 32, "Nothing here", 404)
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/delete").status_code, 200)
        self.rename(job_id, "Too late", 404)

    def test_an_unreadable_run_file_is_never_written_back_from_nothing(self):
        run_dir = self.add_talk(1)
        self.start()
        job_id = self.history()[0]["id"]
        (run_dir / RUN_FILE).write_text("{cut short", encoding="utf-8")  # e.g. a storage error just now
        self.assertIn("Try again", self.rename(job_id, "New name", 503)["error"])
        self.assertEqual((run_dir / RUN_FILE).read_text(encoding="utf-8"), "{cut short")
        self.assertEqual(self.naming(), (None, None))

    def test_a_delete_waits_for_a_name_being_saved(self):
        run_dir = self.add_talk(1)
        self.start()
        job_id = self.history()[0]["id"]
        write, writing, go_on = held_once(server.write_json, RUN_FILE)
        answers = {}

        def ask(action, client, **body):
            answers[action] = client.post(f"/api/jobs/{job_id}/{action}", **body)

        with patch("web.server.write_json", side_effect=write):
            rename = Thread(target=ask, args=("rename", self.client), kwargs={"json": {"title": "Satsang"}})
            rename.start()
            self.assertTrue(writing.wait(5))
            delete = Thread(target=ask, args=("delete", self.browser(STRANGER)))
            delete.start()
            delete.join(0.3)
            self.assertTrue(delete.is_alive())  # not while the name is being written into the folder
            go_on.set()
            rename.join(5)
            delete.join(5)
        self.assertEqual((answers["rename"].status_code, answers["delete"].status_code), (200, 200))
        self.assertEqual(self.history(), [])
        self.start()
        self.assertEqual(self.history(), [])
        self.assertFalse((run_dir / RUN_FILE).exists())
