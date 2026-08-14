"""Model client interface and a scripted stub (Phase E4).

The judges talk to this interface, never to a vendor SDK. Two reasons, and the second is the one
that matters for the experiment:

  * every step of the agent loop -- tool dispatch, budget accounting, degradation, malformed-output
    handling -- is testable at zero cost and with no network, so a CI run proves the harness works
    before a single token is spent;
  * B1, B2 and B3 must be charged in the SAME units. A shared `ModelResponse` carrying
    `tokens_in`/`tokens_out` is what makes "B2 at B3's measured spend" a real constraint rather
    than an assertion in a README.

`ScriptedClient` lives here rather than in tests/ because `e4_judge.py --dry-run` uses it to
exercise a full sweep without an API key, which is the same thing the tests do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    """One model turn. Either it asks for tools or it answers; never neither."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class ModelClient(Protocol):
    """What every judge needs from a model, and nothing more."""

    name: str

    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.0,
        seed: int | None = None,
    ) -> ModelResponse:
        ...


class ScriptedClient:
    """Replays a fixed list of responses. Deterministic, free, offline.

    Records every `messages` list it was handed, so a test can assert on what the judge actually
    showed the model -- which is how the ground-truth-leak check reaches inside the agent loop
    rather than only checking the tool surface.

    Running past the end of the script returns the last response forever rather than raising. A
    runaway loop is a condition the budget is supposed to catch, and a StopIteration here would
    mask the ceiling with a crash and make the test pass for the wrong reason.
    """

    def __init__(self, responses: list[ModelResponse], name: str = "scripted") -> None:
        if not responses:
            raise ValueError("ScriptedClient needs at least one response")
        self.name = name
        self._responses = responses
        self.calls = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    def complete(self, messages, tools=None, temperature=0.0, seed=None) -> ModelResponse:
        self.seen_messages.append(list(messages))
        idx = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[idx]

    @property
    def everything_the_model_saw(self) -> str:
        """Flattened transcript, for leak assertions."""
        import json
        return json.dumps(self.seen_messages, default=str)


def answer(payload: str, tokens_in: int = 400, tokens_out: int = 80) -> ModelResponse:
    """Shorthand: a final answer turn."""
    return ModelResponse(text=payload, tokens_in=tokens_in, tokens_out=tokens_out)


def uses(tool: str, /, *, tokens_in: int = 400, tokens_out: int = 40,
         **arguments) -> ModelResponse:
    """Shorthand: a turn that requests one tool call.

    The first parameter is positional-only and named `tool`, not `name`: `get_param` takes an
    argument called `name`, and `uses("get_param", name="X")` would otherwise bind two values to
    the same parameter and raise. Token counts are keyword-only for the same reason -- a future
    tool with a `tokens_in` argument should not silently become a cost annotation.
    """
    return ModelResponse(
        tool_calls=[ToolCall(id=f"c{abs(hash((tool, tuple(sorted(arguments))))) % 9973}",
                             name=tool, arguments=arguments)],
        tokens_in=tokens_in, tokens_out=tokens_out,
    )
