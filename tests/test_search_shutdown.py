"""Shutting search down: a worker can be stopped, and Python never crashes while one is still busy at exit."""

import gc
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from tests.test_web_search import SampleTalks
from web.search import service as search_service
from web.search.index import TalkIndex

ROOT = Path(__file__).resolve().parents[1]

# A daemon thread keeps querying an index while the main thread exits. Closing the index's database at exit, under
# the busy thread, crashed Python (exit 139 on 3.10, often on 3.14).
BUSY_AT_EXIT = """
import sys, threading, time
sys.path.insert(0, sys.argv[1])
from web.search.index import TalkIndex
index = TalkIndex("model", 8)
count = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c WHERE x < 20000) SELECT sum(x) FROM c"
def busy():
    while True:
        with index.lock:
            index.db.execute(count).fetchone()
threading.Thread(target=busy, daemon=True).start()
time.sleep(0.2)
"""


class TestExit(unittest.TestCase):
    def test_exiting_while_a_thread_uses_an_index_does_not_crash(self):
        for _ in range(5):
            done = subprocess.run([sys.executable, "-c", BUSY_AT_EXIT, str(ROOT)], capture_output=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr.decode(errors="replace")[-500:])

    def test_a_dropped_index_still_closes_its_database(self):
        index = TalkIndex("model", 8)
        db = index.db
        del index
        gc.collect()
        with self.assertRaises(sqlite3.ProgrammingError):
            db.execute("SELECT 1")


class TestStop(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.talks = SampleTalks()
        self.service = search_service.SearchService(
            folder=Path(temp.name) / ".search", talks=self.talks.talks, names=self.talks.names, read=self.talks.read,
            stamp=self.talks.stamp,
        )  # fmt: skip
        self.addCleanup(self.service.stop)

    def test_stop_ends_a_waiting_worker_and_nothing_starts_another(self):
        self.service.start()
        self.assertTrue(self.service.wait_idle(5))
        worker = self.service.worker
        self.assertTrue(worker.is_alive())  # waiting to save the new index
        self.assertTrue(self.service.stop())
        self.assertFalse(worker.is_alive())
        self.assertIsNone(self.service.index)
        self.service.touch("t1")
        self.service.start()
        self.assertIs(self.service.worker, worker)
        self.assertFalse(worker.is_alive())
        self.assertEqual(self.service.search("seva")["talks"], [])

    def test_a_busy_worker_keeps_its_index_until_it_ends(self):
        self.service.start()
        self.assertTrue(self.service.wait_idle(5))
        reading, go_on = threading.Event(), threading.Event()
        real = self.talks.read

        def slow(talk):
            reading.set()
            go_on.wait(5)
            return real(talk)

        self.service._read = slow
        self.service.touch("t1")
        self.assertTrue(reading.wait(5))
        self.assertFalse(self.service.stop(timeout=0.1))
        index = self.service.index
        self.assertEqual(index.db.execute("SELECT 1").fetchone(), (1,))  # still open
        go_on.set()
        self.service.worker.join(5)
        self.assertFalse(self.service.worker.is_alive())
        self.assertTrue(self.service.stop())
        self.assertIsNone(self.service.index)


if __name__ == "__main__":
    unittest.main()
