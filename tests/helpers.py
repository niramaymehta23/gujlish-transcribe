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
