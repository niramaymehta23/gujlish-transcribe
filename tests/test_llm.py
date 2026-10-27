"""The LLM layer against fake OpenAI-compatible and Anthropic servers (no network, no keys)."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

from asr.llm import ClaudeLLM, GeminiLLM, LLMError, LocalLLM, make_llm, parse_json_object, strip_reasoning


class FakeServer:
    """Serves scripted responses and records every request. handler(path, payload, headers) -> (status, body)."""

    def __init__(self, handler):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, payload):
                outer.requests.append((self.path, payload, dict(self.headers)))
                status, body = handler(self.path, payload, dict(self.headers))
                data = json.dumps(body).encode()
                self.send_response(status)
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


class TestLocal(unittest.TestCase):
    def test_complete_and_automatic_model_choice(self):
        def handler(path, payload, headers):
            if path.endswith("/models"):
                return 200, {"data": [{"id": "only-model"}]}
            return 200, {"choices": [{"message": {"content": "<think>x</think>Hello"}, "finish_reason": "stop"}]}

        server = FakeServer(handler)
        self.addCleanup(server.close)
        llm = LocalLLM(base_url=server.url)
        llm.prepare()
        self.assertEqual(llm.label, "local:only-model")
        result = llm.complete("sys", "user", max_tokens=99, temperature=0.1)
        self.assertEqual((result.text, result.truncated), ("Hello", False))
        _, payload, _ = server.requests[-1]
        self.assertEqual(payload["model"], "only-model")
        self.assertEqual(payload["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(payload["max_tokens"], 99)
        self.assertEqual(payload["temperature"], 0.1)
        self.assertNotIn("reasoning_effort", payload)

    def test_truncation_flag(self):
        server = FakeServer(lambda *a: (200, {"choices": [{"message": {"content": "cut"}, "finish_reason": "length"}]}))
        self.addCleanup(server.close)
        self.assertTrue(LocalLLM("m", server.url).complete("s", "u").truncated)

    def test_several_models_require_a_choice(self):
        server = FakeServer(lambda *a: (200, {"data": [{"id": "a"}, {"id": "b"}]}))
        self.addCleanup(server.close)
        with self.assertRaisesRegex(LLMError, "--llm-model.*a, b"):
            LocalLLM(base_url=server.url).prepare()
        self.assertEqual(LocalLLM(base_url=server.url).models(), ["a", "b"])

    def test_nothing_listening_fails_fast_with_a_hint(self):
        probe = HTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
        port = probe.server_port
        probe.server_close()
        llm = LocalLLM(base_url=f"http://127.0.0.1:{port}")
        self.assertIsNone(llm.models())
        with patch("time.sleep") as sleep, self.assertRaisesRegex(LLMError, "Ollama"):
            llm.prepare()
        sleep.assert_not_called()  # a refused connection is not retried

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
            self.assertEqual(LocalLLM("m", server.url).complete("s", "u").text, "ok")
        self.assertEqual(len(calls), 3)


class TestClaude(unittest.TestCase):
    def reply(self, text="Hi", stop="end_turn"):
        return {"content": [{"type": "thinking", "thinking": "x"}, {"type": "text", "text": text}], "stop_reason": stop}

    def test_request_shape_and_key_handling(self):
        server = FakeServer(lambda *a: (200, self.reply(" Hello ")))
        self.addCleanup(server.close)
        llm = ClaudeLLM(base_url=server.url, api_key="secret-key")
        llm.prepare()
        self.assertEqual(llm.complete("sys", "usr").text, "Hello")
        path, payload, headers = server.requests[0]
        self.assertEqual(path, "/v1/messages")
        headers = {k.lower(): v for k, v in headers.items()}  # urllib capitalises header names
        self.assertEqual(headers["x-api-key"], "secret-key")
        self.assertEqual(headers["anthropic-version"], "2023-06-01")
        self.assertEqual(payload["system"], "sys")
        self.assertEqual(payload["model"], "claude-sonnet-5-5")
        self.assertNotIn("secret-key", llm.describe() + llm.label)

    def test_missing_key(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(LLMError, "ANTHROPIC_API_KEY"):
                ClaudeLLM().prepare()
            with self.assertRaisesRegex(LLMError, "ANTHROPIC_API_KEY"):
                ClaudeLLM().complete("s", "u")

    def test_temperature_rejection_is_retried_once_and_remembered(self):
        def handler(path, payload, headers):
            if "temperature" in payload:
                return 400, {"error": {"message": "`temperature` is not supported for this model"}}
            return 200, self.reply()

        server = FakeServer(handler)
        self.addCleanup(server.close)
        llm = ClaudeLLM(base_url=server.url, api_key="k")
        self.assertEqual(llm.complete("s", "u").text, "Hi")
        self.assertEqual(llm.complete("s", "u").text, "Hi")
        self.assertEqual([("temperature" in p) for _, p, _ in server.requests], [True, False, False])

    def test_other_400_is_not_swallowed_and_truncation_is_reported(self):
        server = FakeServer(lambda *a: (400, {"error": {"message": "bad model"}}))
        self.addCleanup(server.close)
        with self.assertRaises(LLMError) as caught:
            ClaudeLLM(base_url=server.url, api_key="k").complete("s", "u")
        self.assertEqual(caught.exception.status, 400)
        server2 = FakeServer(lambda *a: (200, self.reply("partial", "max_tokens")))
        self.addCleanup(server2.close)
        self.assertTrue(ClaudeLLM(base_url=server2.url, api_key="k").complete("s", "u").truncated)

    def test_gemini_uses_a_bearer_key_and_leaves_room_for_reasoning(self):
        server = FakeServer(lambda *a: (200, {"choices": [{"message": {"content": "Hello"}, "finish_reason": "stop"}]}))
        self.addCleanup(server.close)
        llm = GeminiLLM("gemini-test", server.url, api_key="g-key")
        llm.prepare()
        self.assertEqual(llm.complete("sys", "usr", max_tokens=500).text, "Hello")
        path, payload, headers = server.requests[0]
        self.assertEqual(path, "/chat/completions")
        self.assertEqual({k.lower(): v for k, v in headers.items()}["authorization"], "Bearer g-key")
        self.assertEqual(payload["model"], "gemini-test")
        self.assertGreater(payload["max_tokens"], 500)
        self.assertNotIn("temperature", payload)
        self.assertEqual(payload["reasoning_effort"], "low")
        self.assertEqual((llm.label, llm.sends_text_off_device), ("gemini:gemini-test", True))
        self.assertNotIn("g-key", llm.describe())

    def test_gemini_key_sources(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(LLMError, "GEMINI_API_KEY"):
                GeminiLLM().prepare()
        with patch.dict("os.environ", {"GOOGLE_API_KEY": "alt"}, clear=True):
            GeminiLLM().prepare()

    def test_factory(self):
        self.assertIsInstance(make_llm("gemini"), GeminiLLM)
        self.assertIsInstance(make_llm("local"), LocalLLM)
        self.assertIsInstance(make_llm("claude", "m"), ClaudeLLM)
        with self.assertRaises(ValueError):
            make_llm("other")


if __name__ == "__main__":
    unittest.main()
