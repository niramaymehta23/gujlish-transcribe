"""The Gemini client against a fake OpenAI-compatible server (no network, no real keys)."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from asr.llm import GeminiLLM, LLMError, parse_json_object, strip_reasoning


class FakeServer:
    """Serves scripted responses and records every request.

    handler(path, payload, headers) -> (status, body) or (status, body, response_headers).
    """

    def __init__(self, handler):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, payload):
                outer.requests.append((self.path, payload, dict(self.headers)))
                status, body, *extra = handler(self.path, payload, dict(self.headers))
                data = json.dumps(body).encode()
                self.send_response(status)
                for name, value in (extra[0] if extra else {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._serve(None)

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self._serve(json.loads(self.rfile.read(length)))

            def log_message(self, *args):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class TestParsing(unittest.TestCase):
    def test_strip_reasoning(self):
        self.assertEqual(strip_reasoning("<think>hmm\nmore</think>\nHello"), "Hello")

    def test_parse_json_variants(self):
        self.assertEqual(parse_json_object('{"a": 1}'), {"a": 1})
        self.assertEqual(parse_json_object('Sure!\n```json\n{"a": [1, 2]}\n```\nDone'), {"a": [1, 2]})
        self.assertEqual(parse_json_object('<think>{"x":0}</think>{"a": "b"}'), {"a": "b"})
        for bad in ("no json here", '{"a": ', "[1, 2]"):
            with self.assertRaises(LLMError):
                parse_json_object(bad)


class TestGemini(unittest.TestCase):
    def test_request_shape_bearer_key_and_room_for_reasoning(self):
        reply = {"choices": [{"message": {"content": "<think>x</think>Hello"}, "finish_reason": "stop"}]}
        server = FakeServer(lambda *a: (200, reply))
        self.addCleanup(server.close)
        llm = GeminiLLM("gemini-test", server.url, api_key="g-key")
        llm.prepare()
        self.assertEqual(llm.complete("sys", "usr", max_tokens=500).text, "Hello")
        path, payload, headers = server.requests[0]
        self.assertEqual(path, "/chat/completions")
        self.assertEqual({k.lower(): v for k, v in headers.items()}["authorization"], "Bearer g-key")
        self.assertEqual(payload["model"], "gemini-test")
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "sys"})
        self.assertGreater(payload["max_tokens"], 500)
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual(llm.label, "gemini:gemini-test")
        self.assertNotIn("g-key", llm.describe() + llm.label)

    def test_truncation_flag(self):
        server = FakeServer(lambda *a: (200, {"choices": [{"message": {"content": "cut"}, "finish_reason": "length"}]}))
        self.addCleanup(server.close)
        self.assertTrue(GeminiLLM("m", server.url, api_key="k").complete("s", "u").truncated)

    def test_key_sources_and_missing_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(LLMError, "GEMINI_API_KEY"):
                GeminiLLM().prepare()
            with self.assertRaisesRegex(LLMError, "GEMINI_API_KEY"):
                GeminiLLM().complete("s", "u")
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "alt"}, clear=True):
            GeminiLLM().prepare()

    def test_nothing_listening_fails_fast(self):
        probe = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = probe.server_port
        probe.server_close()
        llm = GeminiLLM("m", f"http://127.0.0.1:{port}", api_key="k")
        with patch("time.sleep") as sleep, self.assertRaisesRegex(LLMError, "Could not reach"):
            llm.complete("s", "u")
        sleep.assert_not_called()  # a refused connection is not retried

    def test_errors_never_include_the_key(self):
        server = FakeServer(lambda *a: (400, {"error": {"message": "bad model"}}))
        self.addCleanup(server.close)
        with self.assertRaises(LLMError) as caught:
            GeminiLLM("m", server.url, api_key="g-secret").complete("s", "u")
        self.assertEqual(caught.exception.status, 400)
        self.assertNotIn("g-secret", str(caught.exception))

    def test_server_errors_are_retried(self):
        calls = []

        def handler(path, payload, headers):
            calls.append(1)
            if len(calls) < 3:
                return 503, {"error": "busy"}
            return 200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

        server = FakeServer(handler)
        self.addCleanup(server.close)
        with patch("time.sleep"):
            self.assertEqual(GeminiLLM("m", server.url, api_key="k").complete("s", "u").text, "ok")
        self.assertEqual(len(calls), 3)

    def test_rate_limits_honor_retry_after_and_get_more_attempts(self):
        calls = []

        def handler(path, payload, headers):
            calls.append(1)
            if len(calls) <= 5:
                return 429, {"error": "slow down"}, {"Retry-After": "7"}
            return 200, {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

        server = FakeServer(handler)
        self.addCleanup(server.close)
        with patch("time.sleep") as sleep:
            self.assertEqual(GeminiLLM("m", server.url, api_key="k").complete("s", "u").text, "ok")
        self.assertEqual(len(calls), 6)  # more than the 3 retries ordinary errors get
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [7.0] * 5)

    def test_timeouts_are_retried_only_once(self):
        with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")) as opened, patch("time.sleep"):
            with self.assertRaisesRegex(LLMError, "Could not reach"):
                GeminiLLM("m", "http://127.0.0.1:9", api_key="k").complete("s", "u")
        self.assertEqual(opened.call_count, 2)

    def test_odd_response_shapes_become_clean_errors(self):
        replies = iter(
            [
                {
                    "choices": [
                        {"message": {"content": [{"type": "text", "text": "Hel"}, {"type": "text", "text": "lo"}]}}
                    ]
                },
                {"choices": [{"message": {"content": 42}}]},
                [1, 2, 3],
            ]
        )
        server = FakeServer(lambda path, payload, headers: (200, next(replies)))
        self.addCleanup(server.close)
        llm = GeminiLLM("m", server.url, api_key="k")
        self.assertEqual(llm.complete("s", "u").text, "Hello")  # content sent as a list of parts
        for _ in range(2):
            with self.assertRaises(LLMError):
                llm.complete("s", "u")


if __name__ == "__main__":
    unittest.main()
