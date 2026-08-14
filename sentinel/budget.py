"""Spend ceiling for the agent tier (Phase E4).

Two jobs, and the second one matters more than it looks.

**Job 1 — stop a runaway loop.** An agent that keeps calling tools because it cannot decide is
the failure mode that turns a $0.002 escalation into a $2 one, and the spec prices the LLM tier
per escalation. The ceiling is per bundle, not per fleet-hour, because that is the unit an
operator can reason about.

**Job 2 — make the baselines comparable.** B2 exists to answer "does repeated sampling beat an
agent at the same cost", which is only meaningful if cost is measured in the same units by both.
The budget object is what defines those units, so B3's recorded spend is what sets B2's sample
count (`k_for_budget`).

Nothing here raises. A tripped ceiling degrades the judge to B0's answer and sets
`Verdict.degraded`, because a spend trip is a *result* -- the degradation rate is published
alongside accuracy -- while an exception would look like a crash and lose the partial work.
A judge that wins by exceeding its ceiling has not won.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Budget:
    """Per-bundle ceiling. One budget per judgement, never shared across bundles.

    Defaults are deliberately generous for a first measurement: the point of L1 is to find out
    what the agent actually costs, not to constrain it to a number chosen before anything was
    measured. Tighten these once `verdicts.json` says what the real distribution looks like.
    """

    max_tokens: int = 20_000
    max_calls: int = 12
    max_wall_s: float = 60.0

    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    _t0: float | None = field(default=None, repr=False)
    _trip_reason: str = field(default="", repr=False)

    def start(self) -> "Budget":
        self._t0 = time.monotonic()
        return self

    # ---- accounting -----------------------------------------------------------------

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    @property
    def wall_s(self) -> float:
        return 0.0 if self._t0 is None else time.monotonic() - self._t0

    def charge(self, tokens_in: int = 0, tokens_out: int = 0) -> None:
        """Record what a completed call cost.

        Charged AFTER the call, not before: the input size is knowable in advance but the output
        size is not, and a ceiling enforced on an estimate would either block affordable calls or
        let expensive ones through. The consequence is that the ceiling can be overshot by one
        call's output -- which is why `max_tokens` is a ceiling on spend, not a hard cap on any
        single request.
        """
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.calls += 1

    # ---- the gate -------------------------------------------------------------------

    @property
    def tripped(self) -> bool:
        return bool(self.reason)

    @property
    def reason(self) -> str:
        """Why the ceiling tripped, or empty. Checked before each call, not after.

        Order is fixed so the reported reason is stable across runs: tokens, then calls, then
        wall clock. An agent that blew two limits at once should not report a different cause
        depending on scheduling.
        """
        if self._trip_reason:
            return self._trip_reason
        if self.tokens >= self.max_tokens:
            return f"token ceiling: {self.tokens} >= {self.max_tokens}"
        if self.calls >= self.max_calls:
            return f"call ceiling: {self.calls} >= {self.max_calls}"
        if self._t0 is not None and self.wall_s >= self.max_wall_s:
            return f"wall-clock ceiling: {self.wall_s:.1f}s >= {self.max_wall_s}s"
        return ""

    def trip(self, reason: str) -> None:
        """Force a trip for a reason the counters cannot see -- a malformed response the agent
        cannot parse, a tool loop it cannot escape. Recorded as harness attribution, not model."""
        self._trip_reason = reason

    def snapshot(self) -> dict:
        """The cost fields a Verdict carries. Same units for every judge -- that is the point."""
        return {
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "calls": self.calls,
            "wall_ms": self.wall_s * 1000.0,
        }


def k_for_budget(target_tokens: int, tokens_per_sample: int, max_k: int = 32) -> int:
    """How many samples B2 may draw to match B3's measured spend.

    Floor, not round: overshooting the target would let the "cheap baseline" outspend the agent
    it is supposed to be compared against, which inverts the comparison it exists to make. At
    least 1, because a zero-sample judge has no answer -- if one sample already exceeds the
    target, that is reported as a spend mismatch rather than silently dropped.
    """
    if tokens_per_sample <= 0:
        return 1
    return max(1, min(max_k, target_tokens // tokens_per_sample))


def spend_match(target_tokens: int, actual_tokens: int) -> float:
    """Fractional deviation of B2's achieved spend from B3's. Published, not asserted.

    The plan's rule is a +/-10% match. This returns the number so the README can print what was
    ACHIEVED -- claiming an intended tolerance while reporting nothing is how a controlled
    comparison quietly stops being one.
    """
    if target_tokens <= 0:
        return 0.0
    return (actual_tokens - target_tokens) / target_tokens
