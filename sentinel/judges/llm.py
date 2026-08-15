"""B1 and B2 -- the LLM baselines, and the Anthropic client (Phase E4).

Two corrections the API forced on the plan, both recorded because they change what the numbers
mean rather than merely how they are produced:

**1. There is no temperature knob.** `temperature`, `top_p` and `top_k` are rejected with a 400
on Claude Opus 5. The plan assumed B2 would draw diverse samples by raising temperature; it
cannot. So B2 draws k samples with *identical* inputs and the diversity comes from the model's
own non-determinism (thinking is on by default and is not seeded). That is a weaker sampling
mechanism than the literature assumes, and pretending otherwise would misreport what B2 tested --
so `NSampleJudge` measures and publishes its observed **disagreement rate**. If k samples never
disagree, B2 has collapsed into B1 at k times the cost, and that is the finding.

**2. There is no seed.** The Messages API has no seed parameter. `Verdict.seed` therefore records
the sample index, not a reproducibility guarantee. Re-running this sweep will not reproduce the
verdicts exactly; only the *bundles* are deterministic. Said plainly here so the README cannot
quietly claim end-to-end reproducibility.

Two things the API gives us that improve on the plan:

**Structured outputs.** `output_config.format` constrains the reply to a JSON schema, so B1/B2
cannot emit unparseable output at all. The regex extraction in `agent.py` stays only for B3,
whose tool loop is incompatible with a constrained final format.

**Honest token accounting.** `usage.input_tokens` is the *uncached remainder* -- total prompt
size is `input_tokens + cache_creation_input_tokens + cache_read_input_tokens`. Counting only
`input_tokens` would under-report any judge that benefits from cache hits and silently rig the
matched-budget comparison that B2 exists to make.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from typing import Any

from sentinel.budget import Budget
from sentinel.bundle import RunBundle
from sentinel.judges import Citation, Verdict
from sentinel.judges.deterministic import DeterministicJudge
from sentinel.judges.model import ModelClient, ModelResponse, ToolCall
from sentinel.judges.prompts import system_prompt
from sentinel.judges.tools import BundleTools

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 8000
# Thinking is ON by default on Claude Opus 5 and max_tokens caps thinking + response together,
# so a tight budget truncates mid-answer. `medium` keeps a ~430-call sweep affordable; raise it
# with --effort and report which level produced the table.
DEFAULT_EFFORT = "medium"

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "root_cause": {"type": ["string", "null"]},
        "symptoms": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "metric": {"type": "string"},
                    "t": {"type": "number"},
                    "value": {"type": ["number", "null"]},
                },
                # `t` is optional: a judge reading a timestamp-free tool anchors on
                # `value` instead. check_citations rejects a citation carrying neither.
                "required": ["metric"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["root_cause", "symptoms", "confidence", "rationale", "citations"],
    "additionalProperties": False,
}


class AnthropicClient:
    """Adapts the Messages API to the `ModelClient` protocol every judge speaks.

    Deliberately thin. The judges own the loop, the budget and the scoring; this only translates
    message shapes and reports cost in the units `Budget` expects.
    """

    name = DEFAULT_MODEL

    def __init__(self, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS,
                 effort: str = DEFAULT_EFFORT, structured: bool = False) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - environment problem, not logic
            raise RuntimeError(
                "pip install anthropic -- or run scripts/e4_judge.py --dry-run") from exc
        self._anthropic = anthropic
        self._client = anthropic.Anthropic()
        self.name = model
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort
        # B1/B2 constrain the reply to VERDICT_SCHEMA. B3 cannot: a constrained final format is
        # incompatible with a tool-use loop, so it keeps agent.py's tolerant parser.
        self.structured = structured

    # ---- protocol ---------------------------------------------------------------------

    def complete(self, messages, tools=None, temperature: float = 0.0,
                 seed: int | None = None) -> ModelResponse:
        """`temperature` and `seed` are accepted for protocol compatibility and NOT sent.

        Both are rejected or absent on Claude Opus 5. Silently dropping them here (rather than
        removing them from the protocol) keeps `ScriptedClient` and this client interchangeable
        in tests, while guaranteeing no request can carry a parameter that 400s.
        """
        system, turns = self._split(messages)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": turns,
            "output_config": {"effort": self.effort},
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [self._tool_spec(t) for t in tools]
        elif self.structured:
            kwargs["output_config"]["format"] = {
                "type": "json_schema", "schema": VERDICT_SCHEMA}

        response = self._client.messages.create(**kwargs)
        return self._to_model_response(response)

    # ---- translation ------------------------------------------------------------------

    @staticmethod
    def _split(messages: list[dict]) -> tuple[str, list[dict]]:
        """Split the judges' generic message list into (system, turns).

        Tool results become `user` turns carrying `tool_result` blocks, which is how the
        Messages API represents them -- the judges use an OpenAI-ish `role: "tool"` shape
        internally because it is easier to assert on in tests.
        """
        system_parts: list[str] = []
        turns: list[dict] = []

        for m in messages:
            role = m.get("role")
            if role == "system":
                system_parts.append(str(m.get("content", "")))
            elif role == "tool":
                turns.append({"role": "user", "content": [{
                    "type": "tool_result",
                    "tool_use_id": m["tool_call_id"],
                    "content": str(m.get("content", "")),
                }]})
            elif role == "assistant" and m.get("tool_calls"):
                turns.append({"role": "assistant", "content": [{
                    "type": "tool_use", "id": c["id"], "name": c["name"],
                    "input": c.get("arguments") or {},
                } for c in m["tool_calls"]]})
            else:
                turns.append({"role": role or "user", "content": m.get("content", "")})

        # Consecutive tool results must be merged: the API takes all results for one assistant
        # turn in a single user message, and splitting them trains the model out of parallel
        # tool calls.
        merged: list[dict] = []
        for turn in turns:
            if (merged and turn["role"] == "user" and merged[-1]["role"] == "user"
                    and isinstance(turn["content"], list)
                    and isinstance(merged[-1]["content"], list)):
                merged[-1]["content"].extend(turn["content"])
            else:
                merged.append(turn)
        return "\n\n".join(system_parts), merged

    @staticmethod
    def _tool_spec(spec: dict) -> dict:
        return {"name": spec["name"], "description": spec["description"],
                "input_schema": spec["parameters"]}

    @staticmethod
    def _to_model_response(response) -> ModelResponse:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name,
                                      arguments=dict(block.input or {})))

        usage = response.usage
        # input_tokens is the UNCACHED remainder. Total prompt size includes both cache fields;
        # omitting them would under-report any judge that gets cache hits and break the
        # matched-budget comparison.
        tokens_in = (getattr(usage, "input_tokens", 0)
                     + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
                     + (getattr(usage, "cache_read_input_tokens", 0) or 0))

        return ModelResponse(
            text="".join(text_parts) or None,
            tool_calls=calls,
            tokens_in=tokens_in,
            tokens_out=getattr(usage, "output_tokens", 0),
        )


def build_default_client(model: str | None = None, structured: bool = False) -> ModelClient:
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")):
        raise RuntimeError(
            "no ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN set. Run `ant auth login`, export a "
            "key, or use --dry-run to exercise the sweep at zero cost.")
    return AnthropicClient(model=model or DEFAULT_MODEL, structured=structured)


# --- B1 ------------------------------------------------------------------------------------

class SingleShotJudge:
    """One call, one answer, no tools. The naive baseline most demos stop at."""

    id = "B1"

    def __init__(self, client: ModelClient) -> None:
        self.client = client

    def judge(self, bundle: RunBundle, budget: Budget | None = None,
              variant: str = "v1") -> Verdict:
        budget = (budget or Budget()).start()
        t0 = time.perf_counter()
        parsed, resp = _one_shot(self.client, bundle, variant, budget)
        if parsed is None:
            return _degrade(self.id, bundle, budget, variant, t0,
                            "model returned no parseable verdict")
        return _verdict(self.id, bundle, budget, variant, t0, parsed, seed=0)


# --- B2 ------------------------------------------------------------------------------------

class NSampleJudge:
    """k samples, majority vote, held to B3's measured token spend.

    The comparison this exists to make is *at equal cost*, so `k` is chosen by
    `budget.k_for_budget` from B3's measured spend rather than picked by hand.

    Because sampling parameters are rejected on this model (see the module docstring), the k
    samples are drawn with identical inputs and vary only by the model's own non-determinism.
    `disagreement` records how often they actually differed -- the number that says whether this
    baseline sampled anything at all.
    """

    id = "B2"

    def __init__(self, client: ModelClient, k: int = 3) -> None:
        self.client = client
        self.k = max(1, k)

    def judge(self, bundle: RunBundle, budget: Budget | None = None,
              variant: str = "v1") -> Verdict:
        budget = (budget or Budget()).start()
        t0 = time.perf_counter()

        answers: list[str | None] = []
        parsed_by_answer: dict[str | None, dict] = {}

        for i in range(self.k):
            if budget.tripped:
                break
            parsed, _ = _one_shot(self.client, bundle, variant, budget)
            if parsed is None:
                continue
            root = _norm(parsed.get("root_cause"))
            answers.append(root)
            parsed_by_answer.setdefault(root, parsed)

        if not answers:
            return _degrade(self.id, bundle, budget, variant, t0,
                            "no sample produced a parseable verdict")

        counts = Counter(answers)
        winner, votes = counts.most_common(1)[0]
        chosen = parsed_by_answer[winner]

        verdict = _verdict(self.id, bundle, budget, variant, t0, chosen, seed=None)
        # Reported, not hidden: if disagreement is 0.0 across the sweep, B2 is B1 at k times the
        # cost and the "repeated sampling" baseline never actually sampled.
        verdict.rationale = (f"majority vote {votes}/{len(answers)} of k={self.k} "
                             f"(disagreement {1 - votes / len(answers):.2f}); "
                             f"{verdict.rationale}")[:600]
        verdict.confidence = votes / len(answers)
        return verdict


# --- shared ---------------------------------------------------------------------------------

def _one_shot(client: ModelClient, bundle: RunBundle, variant: str,
              budget: Budget) -> tuple[dict | None, ModelResponse | None]:
    """One constrained call over the same summary B3 starts from.

    The fairness rule from the plan: B1 and B2 see exactly `BundleTools.summarize()`, no more.
    If B3 wins only because it could look further, the experiment measured input size.
    """
    tools = BundleTools(bundle)
    messages = [
        {"role": "system", "content": system_prompt(variant)},
        {"role": "user", "content": json.dumps({"flight": tools.summarize()},
                                               indent=1, default=str)},
    ]
    try:
        resp = client.complete(messages=messages, tools=None)
    except Exception as exc:
        # Same reasoning as agent.py: a transport failure is a harness failure. Returning None
        # lets the caller degrade and be scored as such, instead of aborting the sweep.
        budget.trip(f"{type(exc).__name__}: {str(exc)[:120]}")
        return None, None
    budget.charge(resp.tokens_in, resp.tokens_out)

    if not resp.text:
        return None, resp
    try:
        parsed = json.loads(resp.text)
    except (ValueError, TypeError):
        # Structured outputs make this near-impossible for B1/B2, but a stub client or a
        # future unconstrained path can still land here.
        from sentinel.judges.agent import AgentJudge
        parsed = AgentJudge._parse(resp.text)
    return (parsed if isinstance(parsed, dict) else None), resp


def _verdict(judge_id: str, bundle: RunBundle, budget: Budget, variant: str,
             t0: float, parsed: dict, seed: int | None) -> Verdict:
    citations: list[Citation] = []
    for c in parsed.get("citations") or []:
        if not isinstance(c, dict):
            continue
        try:
            cite = Citation(metric=str(c["metric"]), t=_opt_float(c.get("t")),
                            value=_opt_float(c.get("value")))
        except (KeyError, TypeError, ValueError):
            continue
        # Same rule as agent.py: `t` is optional, but an anchor is not.
        if cite.anchored:
            citations.append(cite)

    snap = budget.snapshot()
    snap["wall_ms"] = (time.perf_counter() - t0) * 1000.0
    return Verdict(
        judge=judge_id,
        bundle_id=bundle.bundle_id,
        prompt_variant=variant,
        root_cause=_norm(parsed.get("root_cause")),
        symptoms=[str(s) for s in (parsed.get("symptoms") or []) if s],
        confidence=_opt_float(parsed.get("confidence")),
        rationale=str(parsed.get("rationale") or "")[:600],
        citations=citations,
        seed=seed,
        **snap,
    )


def _degrade(judge_id: str, bundle: RunBundle, budget: Budget, variant: str,
             t0: float, reason: str) -> Verdict:
    base = DeterministicJudge().judge(bundle)
    snap = budget.snapshot()
    snap["wall_ms"] = (time.perf_counter() - t0) * 1000.0
    return Verdict(
        judge=judge_id, bundle_id=bundle.bundle_id, prompt_variant=variant,
        root_cause=base.root_cause, symptoms=base.symptoms,
        rationale=f"degraded to deterministic baseline: {reason}",
        citations=base.citations, degraded=True, degraded_reason=reason, **snap,
    )


def _norm(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return None if s.lower() in {"", "null", "none", "n/a"} else s


def _opt_float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
