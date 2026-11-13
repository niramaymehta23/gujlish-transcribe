"""Shared test doubles."""

import json


class DeferredExecutor:
    def __init__(self):
        self.tasks = []

    def submit(self, function, *args):
        self.tasks.append((function, args))


class FakeWorker:
    """Stands in for subprocess.Popen: yields the given output lines, then exits with `code`."""

    def __init__(self, lines, code=0, on_wait=None):
        self.stdout = iter(line + "\n" for line in lines)
        self.code = code
        self.on_wait = on_wait

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def wait(self, timeout=None):
        if self.on_wait:
            self.on_wait()
        return self.code


def event(**fields) -> str:
    return "ASR_EVENT " + json.dumps(fields)


def background_check_done(app, timeout: float = 10) -> bool:
    """Wait for the catalogue check a hosted site runs in the background after starting from a saved catalogue (a
    second app on the same storage starts that way). It saves the catalogue, so a test waits for it before its
    storage is removed. True once no check is running."""
    thread = app.extensions.get("asr_reconcile")
    if thread is not None:
        thread.join(timeout)
    return thread is None or not thread.is_alive()


def wait_for_background_check(test, app) -> None:
    """Have `test` wait for the app's background check before the storage made earlier in setUp is removed."""
    test.addCleanup(
        lambda: test.assertTrue(background_check_done(app), "the background catalogue check is still running")
    )
