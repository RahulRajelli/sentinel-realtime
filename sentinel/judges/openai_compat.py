"""OpenAI-compatible client for the judge protocol (Phase E4).

Exists because the strongest open question in this project — *is "the agent reads detection order
as causality" a property of agents, or of gemini-2.5-flash?* — needs a second model, and the
Anthropic path is not available here. Almost every other provider speaks the OpenAI chat
completions API, so one adapter reaches GPT, Grok, DeepSeek, Qwen, Mistral, anything behind
OpenRouter, and local servers (Ollama, vLLM, LM Studio).

Point it anywhere:

    OPENAI_API_KEY=...  --provider openai --model gpt-5.6
    OPENAI_API_KEY=...  --provider openai --model grok-4.6 --base-url https://api.x.ai/v1
    OPENAI_API_KEY=...  --provider openai --model x/y --base-url https://openrouter.ai/api/v1
    OPENAI_API_KEY=none --provider openai --model llama3 --base-url http://localhost:11434/v1

**Whatever runs, the published table must name it.** `client.name` carries model and host, and
`e4_judge.py` writes it into the verdict file. A table that says "an LLM agent" is unfalsifiable,
and this project's entire argument is that its numbers can be checked.

**The translation is nearly a passthrough on purpose.** The judges already speak an OpenAI-ish
message shape internally (`role: "tool"` with `tool_call_id`), so the only real work is nesting
tool calls into `function` objects and back. That mapping is kept as pure staticmethods with no
SDK types, so the tests exercise it without the `openai` package installed.

**Two provider quirks that are handled rather than assumed away:**

* `temperature` and `seed` are sent only when the endpoint is likely to accept them. Several
  reasoning models (o-series, some Grok/Qwen variants) reject `temperature` with a 400, exactly
  as Claude Opus 5 does — see `llm.py`. A rejected parameter would abort a sweep, and a sweep
  that dies on parameter validation teaches nothing about judgement.
* `tool_calls[].arguments` arrives as a JSON *string*, not an object, and a model that emits
  malformed JSON there is common enough to handle. A bad blob becomes `{}` and the tool layer
  returns its error as data, which costs one turn instead of ending the run.
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

from sentinel.judges.model import ModelResponse, ToolCall

DEFAULT_MODEL = "gpt-5.6"
DEFAULT_MAX_TOKENS = 8000
DEFAULT_MIN_INTERVAL_S = 1.0

# Endpoints that reject sampling parameters. Substring match on the model id, because providers
# version them freely ("o3-mini-2025-...", "grok-4.6-reasoning").
_NO_SAMPLING_PARAMS = ("o1", "o3", "o4", "-reasoning", "-thinking")


class OpenAICompatClient:
    """Adapts any OpenAI-compatible chat completions endpoint to `ModelClient`."""

    _rate_lock = threading.Lock()
    _last_call_t = 0.0

    _RETRY_MARKERS = ("429", "rate limit", "RESOURCE_EXHAUSTED", "500", "502", "503", "504",
                      "overloaded", "timeout", "Timeout")

    def __init__(self, model: str = DEFAULT_MODEL, base_url: str | None = None,
                 api_key: str | None = None, max_tokens: int = DEFAULT_MAX_TOKENS,
                 structured: bool = False,
                 min_interval_s: float = DEFAULT_MIN_INTERVAL_S) -> None:
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover - environment, not logic
            raise RuntimeError("pip install openai") from exc

        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise RuntimeError(
                "no OPENAI_API_KEY set. Export one, or pass --api-key. For a local server any "
                "non-empty string works (e.g. OPENAI_API_KEY=none).")
        url = base_url or os.environ.get("OPENAI_BASE_URL") or None

        self._client = OpenAI(api_key=key, base_url=url)
        self.model = model
        self.base_url = url or "api.openai.com"
        # Host is part of the identity: "grok-4.6 on api.x.ai" and the same model via a reseller
        # are not guaranteed to be the same thing, and the table should say which was measured.
        self.name = f"{model} @ {self.base_url}"
        self.max_tokens = max_tokens
        self.structured = structured
        self.min_interval_s = min_interval_s

    # ---- protocol ---------------------------------------------------------------------

    def complete(self, messages, tools=None, temperature: float = 0.0,
                 seed: int | None = None) -> ModelResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._to_openai_messages(messages),
            "max_completion_tokens": self.max_tokens,
        }
        if not any(m in self.model for m in _NO_SAMPLING_PARAMS):
            kwargs["temperature"] = temperature
            if seed is not None:
                kwargs["seed"] = seed
        if tools:
            kwargs["tools"] = [self._tool_spec(t) for t in tools]
        elif self.structured:
            kwargs["response_format"] = {"type": "json_object"}

        resp = self._with_retry(lambda: self._client.chat.completions.create(**kwargs))
        return self._to_model_response(resp)

    # ---- translation (SDK-free, so tests need no `openai` install) ---------------------

    @staticmethod
    def _to_openai_messages(messages: list[dict]) -> list[dict]:
        """Judge messages -> chat completions messages.

        The only structural change is nesting tool calls under `function`. Tool RESULTS are left
        as separate `role: "tool"` messages, unlike the Anthropic and Gemini adapters which must
        merge them: this API keys results by `tool_call_id`, so parallel calls need no merging
        and merging them would in fact be wrong.
        """
        out: list[dict] = []
        for m in messages:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                out.append({
                    "role": "assistant",
                    "content": m.get("content") or None,
                    "tool_calls": [{
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"],
                                     "arguments": json.dumps(c.get("arguments") or {})},
                    } for c in m["tool_calls"]],
                })
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                            "content": str(m.get("content", ""))})
            else:
                out.append({"role": role or "user", "content": m.get("content", "")})
        return out

    @staticmethod
    def _tool_spec(spec: dict) -> dict:
        return {"type": "function", "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": spec.get("parameters") or {"type": "object", "properties": {}},
        }}

    @staticmethod
    def _parse_arguments(raw: Any) -> dict:
        """`arguments` is a JSON string here, and is not always valid JSON.

        A malformed blob becomes {} so the tool layer can answer with its own error as data --
        one wasted turn the model can correct, instead of an exception that ends the judgement
        and gets attributed to the harness.
        """
        if isinstance(raw, dict):
            return raw
        try:
            parsed = json.loads(raw or "{}")
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @classmethod
    def _to_model_response(cls, resp) -> ModelResponse:
        choice = (getattr(resp, "choices", None) or [None])[0]
        msg = getattr(choice, "message", None)

        calls: list[ToolCall] = []
        for tc in (getattr(msg, "tool_calls", None) or []):
            fn = getattr(tc, "function", None)
            calls.append(ToolCall(
                id=getattr(tc, "id", None) or f"{getattr(fn, 'name', 'tool')}_{len(calls)}",
                name=getattr(fn, "name", "unknown"),
                arguments=cls._parse_arguments(getattr(fn, "arguments", None)),
            ))

        u = getattr(resp, "usage", None)
        # Reasoning tokens are billed output and are reported inside a details object on the
        # providers that expose them. Leaving them out would under-report a thinking model and
        # rig B2's matched-spend comparison, the same trap gemini.py documents.
        tokens_out = (getattr(u, "completion_tokens", 0) or 0) if u else 0

        return ModelResponse(
            text=getattr(msg, "content", None) or None,
            tool_calls=calls,
            tokens_in=(getattr(u, "prompt_tokens", 0) or 0) if u else 0,
            tokens_out=tokens_out,
        )

    # ---- pacing and retry, same reasoning as gemini.py --------------------------------

    def _pace(self) -> None:
        if self.min_interval_s <= 0:
            return
        with OpenAICompatClient._rate_lock:
            wait = OpenAICompatClient._last_call_t + self.min_interval_s - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            OpenAICompatClient._last_call_t = time.monotonic()

    def _with_retry(self, call, attempts: int = 6, base_delay: float = 2.0):
        last: Exception | None = None
        for i in range(attempts):
            self._pace()
            try:
                return call()
            except Exception as exc:      # noqa: BLE001 - re-raised below if not retryable
                if not any(m in str(exc) for m in self._RETRY_MARKERS):
                    raise
                last = exc
                if i < attempts - 1:
                    time.sleep(base_delay * (2 ** i))
        raise last  # type: ignore[misc]


def build_openai_client(model: str | None = None, structured: bool = False,
                        base_url: str | None = None, api_key: str | None = None,
                        min_interval_s: float = DEFAULT_MIN_INTERVAL_S) -> OpenAICompatClient:
    return OpenAICompatClient(model=model or DEFAULT_MODEL, base_url=base_url, api_key=api_key,
                              structured=structured, min_interval_s=min_interval_s)
