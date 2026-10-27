"""A small LLM layer for translation and summaries, using only the standard library.

Two providers share one `complete(system, user)` interface:

* ``local``: any OpenAI-compatible server on this computer: Ollama (default, port 11434), LM Studio (1234) or
  ``mlx_lm.server`` (8080). Text never leaves the machine.
* ``claude``: the Anthropic Messages API. Only transcript *text* (never audio) is sent. The key is read from the
  ANTHROPIC_API_KEY environment variable and is deliberately not accepted as a command-line argument.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_LOCAL_URL = "http://localhost:11434/v1"
DEFAULT_CLAUDE_URL = "https://api.anthropic.com"
DEFAULT_CLAUDE_MODEL = "claude-sonnet-5-5"
DEFAULT_GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_GEMINI_MODEL = (
    "gemini-3.8-flash"  # override with --llm-model / GUJLISH_GEMINI_MODEL when a newer model is in use
)
ANTHROPIC_VERSION = "2023-06-01"
RETRY_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}


class LLMError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, body: str = ""):
        super().__init__(message)
        self.status = status
        self.body = body


@dataclass
class Completion:
    text: str
    truncated: bool = False  # the model stopped because it hit max_tokens


def _request(url: str, payload: dict | None, headers: dict, timeout: float, retries: int = 3) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    all_headers = {"Content-Type": "application/json", **headers}
    for attempt in range(retries + 1):
        request = urllib.request.Request(url, data=data, headers=all_headers, method="GET" if data is None else "POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")[:1000]
            if error.code in RETRY_STATUS and attempt < retries:
                time.sleep(min(30.0, 2.0**attempt))
                continue
            raise LLMError(f"HTTP {error.code} from {url}: {body}", error.code, body) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            reason = getattr(error, "reason", error)
            # A refused connection means nothing is listening (e.g. the local server is not started): fail at once.
            if attempt < retries and not isinstance(reason, ConnectionRefusedError):
                time.sleep(min(30.0, 2.0**attempt))
                continue
            raise LLMError(f"Could not reach {url}: {reason}") from None
        except ValueError as error:
            raise LLMError(f"Unreadable response from {url}: {error}") from None
    raise LLMError(f"No response from {url}")  # pragma: no cover


def strip_reasoning(text: str) -> str:
    """Drop <think>…</think> blocks some local models emit before the answer."""
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


class LocalLLM:
    provider = "local"
    sends_text_off_device = False
    token_headroom = 0
    send_temperature = True
    extra_payload: dict = {}

    def __init__(self, model: str | None = None, base_url: str | None = None, timeout: float = 600.0):
        self.base_url = (base_url or os.environ.get("GUJLISH_LLM_URL") or DEFAULT_LOCAL_URL).rstrip("/")
        self.model = model or os.environ.get("GUJLISH_LLM_MODEL") or None
        self.timeout = timeout

    def _headers(self) -> dict:
        return {}

    def models(self) -> list[str] | None:
        """Model ids the server offers, or None when it cannot be reached."""
        try:
            data = _request(f"{self.base_url}/models", None, self._headers(), timeout=3.0, retries=0)
        except LLMError:
            return None
        return sorted(str(item.get("id")) for item in data.get("data", []) if item.get("id"))

    def resolve_model(self) -> str:
        if self.model:
            return self.model
        available = self.models()
        if available is None:
            raise LLMError(
                f"No local LLM server answered at {self.base_url}. Start Ollama (`ollama serve`), LM Studio or "
                "`mlx_lm.server`, set --llm-url if it uses another port, or use --llm claude."
            )
        if len(available) == 1:
            self.model = available[0]
            return self.model
        listing = ", ".join(available) if available else "none"
        raise LLMError(f"Choose a local model with --llm-model. The server at {self.base_url} offers: {listing}")

    def prepare(self) -> None:
        self.resolve_model()

    @property
    def label(self) -> str:
        return f"local:{self.model or '?'}"

    def describe(self) -> str:
        return f"local model {self.model or '(auto)'} at {self.base_url}"

    def complete(self, system: str, user: str, *, max_tokens: int = 2048, temperature: float = 0.2) -> Completion:
        payload = {
            "model": self.resolve_model(),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_tokens": max_tokens + self.token_headroom,
            "stream": False,
            **self.extra_payload,
        }
        if self.send_temperature:
            payload["temperature"] = temperature
        data = _request(f"{self.base_url}/chat/completions", payload, self._headers(), self.timeout)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"Unexpected response from the local server: {str(data)[:300]}") from None
        return Completion(strip_reasoning(text), choice.get("finish_reason") == "length")


class ClaudeLLM:
    provider = "claude"
    sends_text_off_device = True

    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 300.0,
    ):
        self.base_url = (base_url or os.environ.get("ANTHROPIC_BASE_URL") or DEFAULT_CLAUDE_URL).rstrip("/")
        self.model = model or os.environ.get("GUJLISH_CLAUDE_MODEL") or DEFAULT_CLAUDE_MODEL
        self._api_key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        self.timeout = timeout
        self._send_temperature = True

    def prepare(self) -> None:
        if not self._api_key:
            raise LLMError("Set the ANTHROPIC_API_KEY environment variable to use --llm claude / --summary-llm claude.")

    @property
    def label(self) -> str:
        return f"claude:{self.model}"

    def describe(self) -> str:
        return f"Claude API model {self.model} (transcript text is sent to Anthropic; audio is not)"

    def complete(self, system: str, user: str, *, max_tokens: int = 2048, temperature: float = 0.2) -> Completion:
        if not self._api_key:
            raise LLMError("Set the ANTHROPIC_API_KEY environment variable to use --llm claude / --summary-llm claude.")
        headers = {"x-api-key": self._api_key, "anthropic-version": ANTHROPIC_VERSION}
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if self._send_temperature:
            payload["temperature"] = temperature
        try:
            data = _request(f"{self.base_url}/v1/messages", payload, headers, self.timeout)
        except LLMError as error:
            # Some models fix their own sampling; retry once without the parameter and remember that.
            if error.status == 400 and "temperature" in error.body.lower() and self._send_temperature:
                self._send_temperature = False
                payload.pop("temperature", None)
                data = _request(f"{self.base_url}/v1/messages", payload, headers, self.timeout)
            else:
                raise
        text = "".join(
            block.get("text", "")
            for block in data.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        return Completion(text.strip(), data.get("stop_reason") == "max_tokens")


class GeminiLLM(LocalLLM):
    """Google Gemini through its OpenAI-compatible endpoint. Only transcript text is sent; the key comes from the
    GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable and is never a command-line argument."""

    provider = "gemini"
    sends_text_off_device = True
    token_headroom = 4096  # Gemini's internal reasoning can use the output budget before any answer text appears
    send_temperature = False  # Google's Gemini 3.8 migration notes say to remove temperature/top_p/top_k
    # Translation needs fluency, not deep reasoning. "low" is the lowest level Gemini 3.x accepts ("minimal" errors);
    # through the OpenAI-compatible endpoint it maps to thinking_level low.
    extra_payload = {"reasoning_effort": "low"}

    def __init__(self, model: str | None = None, base_url: str | None = None, api_key: str | None = None):
        super().__init__(
            model or os.environ.get("GUJLISH_GEMINI_MODEL") or DEFAULT_GEMINI_MODEL,
            base_url or os.environ.get("GUJLISH_GEMINI_URL") or DEFAULT_GEMINI_URL,
            timeout=300.0,
        )
        self._api_key = api_key if api_key is not None else _gemini_key()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"}

    def prepare(self) -> None:
        if not self._api_key:
            raise LLMError("Set the GEMINI_API_KEY environment variable to use --llm gemini / --summary-llm gemini.")

    def resolve_model(self) -> str:
        self.prepare()
        return self.model

    @property
    def label(self) -> str:
        return f"gemini:{self.model}"

    def describe(self) -> str:
        return f"Gemini API model {self.model} (transcript text is sent to Google; audio is not)"


def _gemini_key() -> str:
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or ""


def gemini_available() -> bool:
    return bool(_gemini_key())


def claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def make_llm(provider: str, model: str | None = None, base_url: str | None = None):
    if provider == "local":
        return LocalLLM(model, base_url)
    if provider == "gemini":
        return GeminiLLM(model)
    if provider == "claude":
        return ClaudeLLM(model)  # endpoint override, if any, comes from ANTHROPIC_BASE_URL
    raise ValueError(f"Unknown LLM provider {provider!r}")
