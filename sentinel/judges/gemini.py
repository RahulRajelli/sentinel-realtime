"""Gemini client for the judge protocol, via Vertex AI (Phase E4).

Used because it authenticates with credentials that already exist on this machine -- Google
application-default credentials -- where the Anthropic path needs a key that is not set and the
Anthropic-on-Vertex path has zero quota allocated (measured 2026-08-14: 429 on every Claude
model, on an otherwise authenticated request).

It implements the same one-method `ModelClient` protocol as everything else, so the judges,
scorer and statistics are untouched. That was the point of making the protocol one method.

**One thing this fixes that the Anthropic path could not.** `temperature` is rejected on Claude
Opus 5, so B2's k samples could only vary by the model's own non-determinism -- a weaker
sampling mechanism than the literature assumes. Gemini accepts `temperature`, so B2 here draws
genuinely diverse samples and the repeated-sampling baseline is the one the papers describe.
`NSampleJudge` still publishes its observed disagreement rate, which is now a measurement of
sampling rather than of jitter.

**What must be said wherever a number from this client is published:** the table measures
*Gemini*, not Claude. A README, a post or a resume line that says "an LLM agent" without naming
the model is not wrong so much as unfalsifiable, and this project's whole argument is that its
numbers can be checked.

Automatic function calling is disabled on purpose. The SDK will happily run the tool loop for
you; letting it would move budget accounting and the degradation path outside `agent.py`, and
those are the two things B2's matched-spend comparison depends on.
"""

from __future__ import annotations

from typing import Any

from sentinel.judges.model import ModelResponse, ToolCall

DEFAULT_MODEL = "gemini-2.5-flash"
DEFAULT_PROJECT = "gen-lang-client-0725459099"
DEFAULT_LOCATION = "global"
DEFAULT_MAX_TOKENS = 8000


class GeminiClient:
    """Adapts Vertex Gemini to `ModelClient`. Thin by design -- judges own the loop."""

    def __init__(self, model: str = DEFAULT_MODEL, project: str = DEFAULT_PROJECT,
                 location: str = DEFAULT_LOCATION, max_tokens: int = DEFAULT_MAX_TOKENS,
                 structured: bool = False) -> None:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - environment, not logic
            raise RuntimeError("pip install google-genai") from exc
        self._genai, self._types = genai, types
        self._client = genai.Client(vertexai=True, project=project, location=location)
        self.name = f"vertex/{model}"
        self.model = model
        self.max_tokens = max_tokens
        self.structured = structured

    # ---- protocol ---------------------------------------------------------------------

    def complete(self, messages, tools=None, temperature: float = 0.0,
                 seed: int | None = None) -> ModelResponse:
        types = self._types
        system, contents = self._split(messages)

        cfg: dict[str, Any] = {
            "max_output_tokens": self.max_tokens,
            "temperature": temperature,
            # The SDK offers to run the tool loop itself. Declining is deliberate: the budget
            # ceiling, the degradation path and the token accounting all live in agent.py, and
            # a loop we do not drive is a loop we cannot charge.
            "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True),
        }
        if system:
            cfg["system_instruction"] = system
        if seed is not None:
            cfg["seed"] = seed          # Gemini honours a seed; Anthropic has none.
        if tools:
            cfg["tools"] = [types.Tool(function_declarations=[self._decl(t) for t in tools])]
        elif self.structured:
            cfg["response_mime_type"] = "application/json"

        resp = self._client.models.generate_content(
            model=self.model, contents=contents,
            config=types.GenerateContentConfig(**cfg))

        return self._to_model_response(resp)

    # ---- translation ------------------------------------------------------------------

    def _split(self, messages: list[dict]) -> tuple[str, list]:
        """Generic judge messages -> (system_instruction, Gemini contents).

        Gemini names the assistant role `model`, and a tool result is a *user*-side
        `function_response` keyed by the function NAME rather than a call id -- so the name is
        carried through `agent.py`'s tool messages for exactly this reason.
        """
        types = self._types
        system_parts: list[str] = []
        contents: list = []

        for m in messages:
            role = m.get("role")
            if role == "system":
                system_parts.append(str(m.get("content", "")))
            elif role == "tool":
                contents.append(types.Content(role="user", parts=[
                    types.Part.from_function_response(
                        name=m.get("name", "unknown"),
                        response={"result": str(m.get("content", ""))})]))
            elif role == "assistant" and m.get("tool_calls"):
                contents.append(types.Content(role="model", parts=[
                    types.Part.from_function_call(name=c["name"], args=c.get("arguments") or {})
                    for c in m["tool_calls"]]))
            else:
                contents.append(types.Content(
                    role="model" if role == "assistant" else "user",
                    parts=[types.Part.from_text(text=str(m.get("content", "")))]))
        return "\n\n".join(system_parts), contents

    @staticmethod
    def _decl(spec: dict) -> dict:
        """Tool spec -> Gemini function declaration.

        A parameterless tool must omit `parameters` entirely: an object schema with no
        properties is rejected, which is why `list_advisories` needs the special case.
        """
        params = spec.get("parameters") or {}
        decl: dict[str, Any] = {"name": spec["name"], "description": spec["description"]}
        if params.get("properties"):
            decl["parameters"] = params
        return decl

    def _to_model_response(self, resp) -> ModelResponse:
        text_parts: list[str] = []
        calls: list[ToolCall] = []

        for cand in (resp.candidates or []):
            content = getattr(cand, "content", None)
            for i, part in enumerate(getattr(content, "parts", None) or []):
                if getattr(part, "text", None):
                    text_parts.append(part.text)
                fc = getattr(part, "function_call", None)
                if fc is not None:
                    # Gemini issues no call id, so one is synthesised. It only has to be stable
                    # within a turn -- agent.py pairs the result back by this value.
                    calls.append(ToolCall(id=f"{fc.name}_{i}", name=fc.name,
                                          arguments=dict(fc.args or {})))

        u = getattr(resp, "usage_metadata", None)
        # `thoughts_token_count` is billed output on thinking models and is reported separately
        # from candidates; leaving it out would under-report B3 and quietly rig the matched-spend
        # comparison in the agent's favour.
        tokens_out = ((getattr(u, "candidates_token_count", 0) or 0)
                      + (getattr(u, "thoughts_token_count", 0) or 0)) if u else 0

        return ModelResponse(
            text="".join(text_parts) or None,
            tool_calls=calls,
            tokens_in=(getattr(u, "prompt_token_count", 0) or 0) if u else 0,
            tokens_out=tokens_out,
        )


def build_gemini_client(model: str | None = None, structured: bool = False) -> GeminiClient:
    return GeminiClient(model=model or DEFAULT_MODEL, structured=structured)
