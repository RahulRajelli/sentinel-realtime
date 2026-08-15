"""B3 -- the tool-using agent (Phase E4). This is what the whole plan is for.

The loop is deliberately small: show the model the same summary every other judge starts from,
let it call read-only tools over the frozen bundle, and stop when it answers or when the ceiling
trips. There is no reflection step, no self-critique pass, and no evolved scaffold -- *Sample
More Reflect Less* and *Harness Evolution, Rethought* both found those losing to plain repeated
sampling at matched cost, and B2 is in the table precisely to check that claim here. Adding
machinery the literature says does not pay, and then reporting a win, would be the architecture
theater this project exists to reject.

Every exit path produces a scoreable verdict. Three of them are failures, and each is recorded as
what it is rather than swallowed:

  * **ceiling tripped** -- degrade to B0's answer, `degraded=True`. Attributed to HARNESS.
  * **output will not parse** -- retried `MAX_PARSE_RETRIES` times, then trip the budget with a
    reason. Also HARNESS: the model had the evidence, the harness could not read the answer.
  * **model answers with no tool calls at all** -- allowed. It is a legitimate strategy and the
    scorer will judge it on citations like any other; an agent forced to call tools would be
    measuring compliance, not judgement.

What this file must never do is look at `bundle.expected_root_cause`. The tools are an allow-list
(`tools.py`) and the prompts name no fault types, so the label is unreachable by construction --
`test_agent.py` asserts it against the full transcript the model actually saw.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from sentinel.budget import Budget
from sentinel.bundle import RunBundle
from sentinel.judges import Citation, Verdict
from sentinel.judges.deterministic import DeterministicJudge
from sentinel.judges.model import ModelClient
from sentinel.judges.prompts import system_prompt
from sentinel.judges.tools import BundleTools

MAX_PARSE_RETRIES = 2

# Matches the outermost {...} in a reply. Models wrap JSON in prose or fences often enough that
# refusing anything but a bare object would score prose-wrapping as a reasoning failure.
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class AgentJudge:
    id = "B3"

    def __init__(self, client: ModelClient, max_turns: int = 10,
                 withhold: tuple[str, ...] = (), offer: tuple[str, ...] = ()) -> None:
        self.client = client
        self.max_turns = max_turns
        # Tools removed from the offered set, for ablation.
        #
        # Exists because of the E4 result on 2026-08-14: B3 scored 0.00 on `compass_offset` while
        # B1 -- same model, same prompt, NO tools -- scored 0.89, and B3's rationale said "the EKF
        # inconsistency was detected first... this likely caused the compass inconsistency". The
        # hypothesis is that `list_advisories` and `ordering` expose detection ORDER, and the
        # model reads order as causality. Withholding them tests that directly instead of
        # inferring it from rationales.
        #
        # Withheld tools are removed from the offered specs, not stubbed: a tool that exists and
        # refuses would still tell the model the ordering question is askable.
        self.withhold = tuple(withhold)
        # Opt-in extras from OPTIONAL_SPECS. They are not in SPECS so that the default offered
        # set stays exactly the five the published table was measured against.
        self.offer = tuple(offer)
        self.specs = ([s for s in BundleTools.SPECS if s["name"] not in self.withhold]
                      + [s for s in BundleTools.OPTIONAL_SPECS if s["name"] in self.offer])

    def judge(self, bundle: RunBundle, budget: Budget | None = None,
              variant: str = "v1") -> Verdict:
        budget = (budget or Budget()).start()
        tools = BundleTools(bundle)
        t0 = time.perf_counter()

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(variant)},
            {"role": "user", "content": json.dumps(
                {"flight": tools.summarize()}, indent=1, default=str)},
        ]

        parse_failures = 0

        for _ in range(self.max_turns):
            # Checked BEFORE the call, not after: charging for a call the ceiling already
            # forbade would make the published spend exceed the published ceiling.
            if budget.tripped:
                return self._degrade(bundle, budget, variant, budget.reason, t0)

            try:
                resp = self.client.complete(
                    messages=messages, tools=self.specs, temperature=0.0, seed=0)
            except Exception as exc:
                # A transport failure -- rate limit, timeout, 5xx -- is a HARNESS failure, not a
                # model one, and it must not take the sweep down with it. Measured 2026-08-14:
                # a ~430-call sweep hits provider rate limits reliably, and an uncaught 429 was
                # killing the whole run and losing every judgement before it.
                #
                # Degrading here also keeps the attribution honest: score.attribute reads
                # `degraded` as HARNESS, so a rate limit is never charged to the model's
                # reasoning.
                budget.trip(f"{type(exc).__name__}: {str(exc)[:120]}")
                return self._degrade(bundle, budget, variant, budget.reason, t0)
            budget.charge(resp.tokens_in, resp.tokens_out)

            if resp.wants_tools:
                messages.append({"role": "assistant", "tool_calls": [
                    {"id": c.id, "name": c.name, "arguments": c.arguments}
                    for c in resp.tool_calls]})
                for call in resp.tool_calls:
                    # Errors come back as data (tools.py), so a bad call costs a turn and
                    # teaches the model something, instead of ending the run.
                    result = tools.call(call.name, call.arguments)
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "name": call.name,
                                     "content": json.dumps(result, default=str)})
                continue

            parsed = self._parse(resp.text)
            if parsed is None:
                parse_failures += 1
                if parse_failures > MAX_PARSE_RETRIES:
                    budget.trip(f"unparseable output after {parse_failures} attempts")
                    return self._degrade(bundle, budget, variant, budget.reason, t0)
                messages.append({"role": "assistant", "content": resp.text or ""})
                messages.append({"role": "user", "content":
                                 "That was not valid JSON. Reply with the JSON object only."})
                continue

            return self._verdict(bundle, budget, variant, parsed, t0)

        # Ran out of turns without an answer. A ceiling by another name, so it degrades like one.
        budget.trip(f"no answer within {self.max_turns} turns")
        return self._degrade(bundle, budget, variant, budget.reason, t0)

    # ---- exits ----------------------------------------------------------------------

    def _verdict(self, bundle: RunBundle, budget: Budget, variant: str,
                 parsed: dict, t0: float) -> Verdict:
        citations: list[Citation] = []
        for c in parsed.get("citations") or []:
            if not isinstance(c, dict):
                continue
            try:
                cite = Citation(metric=str(c["metric"]), t=_opt_float(c.get("t")),
                                value=_opt_float(c.get("value")))
            except (KeyError, TypeError, ValueError):
                # A malformed citation is dropped, not repaired. Inventing a timestamp or a value
                # to make it parse would manufacture the evidence the scorer is checking for.
                continue
            # `t` is optional now, because evidence_untimed exposes none -- but an unanchored
            # citation points at nothing, so it is dropped here rather than passed to the scorer
            # to fail. Dropping it means "no citation", which attribute() already reads as a
            # harness problem, and that is the honest label.
            if cite.anchored:
                citations.append(cite)

        return Verdict(
            judge=self.id,
            bundle_id=bundle.bundle_id,
            prompt_variant=variant,
            root_cause=_opt_str(parsed.get("root_cause")),
            symptoms=[str(s) for s in (parsed.get("symptoms") or []) if s],
            confidence=_opt_float(parsed.get("confidence")),
            rationale=str(parsed.get("rationale") or "")[:600],
            citations=citations,
            seed=0,
            **_cost(budget, t0),
        )

    def _degrade(self, bundle: RunBundle, budget: Budget, variant: str,
                 reason: str, t0: float) -> Verdict:
        """Fall back to B0's answer, labelled.

        Kept as judge B3 on purpose. Relabelling it B0 would quietly delete B3's failures from
        the table and inflate its accuracy -- the degradation rate is a published number, not an
        embarrassment to hide.
        """
        base = DeterministicJudge().judge(bundle)
        return Verdict(
            judge=self.id,
            bundle_id=bundle.bundle_id,
            prompt_variant=variant,
            root_cause=base.root_cause,
            symptoms=base.symptoms,
            confidence=None,
            rationale=f"degraded to deterministic baseline: {reason}",
            citations=base.citations,
            degraded=True,
            degraded_reason=reason,
            seed=0,
            **_cost(budget, t0),
        )

    # ---- parsing --------------------------------------------------------------------

    @staticmethod
    def _parse(text: str | None) -> dict | None:
        if not text:
            return None
        match = _JSON_BLOCK.search(text)
        if not match:
            return None
        try:
            out = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return out if isinstance(out, dict) else None


def _cost(budget: Budget, t0: float) -> dict[str, Any]:
    snap = budget.snapshot()
    # Wall time is measured across the whole judgement, including tool dispatch, rather than
    # taken from the budget's own clock -- the budget starts a fraction later.
    snap["wall_ms"] = (time.perf_counter() - t0) * 1000.0
    return snap


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    # Models write "null"/"none"/"" for "no fault". Treating those as a fault type would score a
    # correct clean-flight answer as a hallucination.
    return None if s.lower() in {"", "null", "none", "n/a"} else s


def _opt_float(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
