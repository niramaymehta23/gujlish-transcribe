"""A small Gemini client for translation and summaries, using only the standard library.

Requests go to Google's OpenAI-compatible Gemini endpoint. Only transcript *text* is sent (never audio). The key is
read from the GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable and is deliberately not accepted as a
command-line argument, so it never appears in process listings or logs.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_GEMINI_MODEL = (
    "gemini-3.8-flash"  # override with --llm-model / GUJLISH_GEMINI_MODEL when a newer model is in use
)
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504}


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Completion:
    text: str
    truncated: bool = False  # the model stopped because it hit max_tokens


RATE_LIMIT_STATUS = {429}  # "slow down": worth waiting longer for than an ordinary server error
RATE_LIMIT_RETRIES = 6
MAX_WAIT_SECONDS = 60.0


def _wait_seconds(attempt: int, retry_after: str | None = None) -> float:
    """Honor a server's Retry-After (in seconds) when given; otherwise back off exponentially with jitter."""
    if retry_after:
        try:
            return min(MAX_WAIT_SECONDS, max(0.0, float(retry_after)))
        except ValueError:
            pass  # an HTTP date: fall back to backoff
    return min(MAX_WAIT_SECONDS, 2.0**attempt) * random.uniform(0.75, 1.25)


def _request(url: str, payload: dict | None, headers: dict, timeout: float, retries: int = 3) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    all_headers = {"Content-Type": "application/json", **headers}
    attempt = timeouts = 0
    while True:
        request = urllib.request.Request(url, data=data, headers=all_headers, method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                value = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:1000]
            limit = max(retries, RATE_LIMIT_RETRIES) if retries and error.code in RATE_LIMIT_STATUS else retries
            if error.code in RETRY_STATUS and attempt < limit:
                time.sleep(_wait_seconds(attempt, error.headers.get("Retry-After") if error.headers else None))
                attempt += 1
                continue
            raise LLMError(f"HTTP {error.code} from {url}: {body}", error.code, body) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException) as error:
            reason = getattr(error, "reason", error)
            timed_out = isinstance(reason, TimeoutError)
            timeouts += timed_out
            # A refused connection means nothing is listening: fail at once.
            # A timeout is retried only once: each attempt already waited the full timeout, and a paid API may have
            # processed (and billed) the request anyway.
            if attempt < retries and not isinstance(reason, ConnectionRefusedError) and timeouts <= 1:
                time.sleep(_wait_seconds(attempt))
                attempt += 1
                continue
            raise LLMError(f"Could not reach {url}: {reason or type(error).__name__}") from None
        except ValueError as error:
            raise LLMError(f"Unreadable response from {url}: {error}") from None
        if not isinstance(value, dict):
            raise LLMError(f"Unexpected response from {url}: {str(value)[:300]}")
        return value


def _message_text(content: object) -> str:
    """OpenAI-compatible content is usually a string, but some servers send a list of {type, text} parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text"
        )
    raise TypeError(type(content).__name__)


def strip_reasoning(text: str) -> str:
    """Drop any <think>…</think> reasoning block a model emits before the answer."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()


def parse_json_object(text: str) -> dict:
    """Extract the first JSON object from model output, tolerating code fences and surrounding prose."""
    text = strip_reasoning(text)
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise LLMError("The model did not return a JSON object")
    try:
        value = json.loads(text[start : end + 1])
    except json.JSONDecodeError as error:
        raise LLMError(f"The model returned invalid JSON: {error}") from None
    if not isinstance(value, dict):
        raise LLMError("The model returned JSON that is not an object")
    return value


class GeminiLLM:
    provider = "gemini"
    token_headroom = 4096  # Gemini's internal reasoning can use the output budget before any answer text appears
    # Translation needs fluency, not deep reasoning. "low" is the lowest level Gemini 3.x accepts ("minimal" errors);
    # through the OpenAI-compatible endpoint it maps to thinking_level low. No temperature is sent: Google's
    # Gemini 3.8 migration notes say to remove temperature/top_p/top_k.
    extra_payload = {"reasoning_effort": "low"}

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get("GUJLISH_GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        self.base_url = (base_url or os.environ.get("GUJLISH_GEMINI_URL") or DEFAULT_GEMINI_URL).rstrip("/")
        self.timeout = 300.0
        self._api_key = api_key if api_key is not None else gemini_key()

    def prepare(self) -> None:
        if not self._api_key:
            raise LLMError("Set the GEMINI_API_KEY environment variable to translate and summarize (see the README).")

    @property
    def label(self) -> str:
        return f"gemini:{self.model}"

    def describe(self) -> str:
        return f"Gemini API model {self.model} (transcript text is sent to Google; audio is not)"

    def complete(self, system: str, user: str, *, max_tokens: int = 2048, temperature: float = 0.2) -> Completion:
        """`temperature` is accepted for interface stability but not sent (see the class note)."""
        self.prepare()
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens + self.token_headroom,
            "stream": False,
            **self.extra_payload,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        data = _request(f"{self.base_url}/chat/completions", payload, headers, self.timeout)
        try:
            choice = data["choices"][0]
            text = _message_text(choice["message"]["content"])
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"Unexpected response from Gemini: {str(data)[:300]}") from None
        return Completion(strip_reasoning(text), choice.get("finish_reason") == "length")


def gemini_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


def gemini_available() -> bool:
    return bool(gemini_key())
