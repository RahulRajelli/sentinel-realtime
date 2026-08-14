"""Judge interface and verdict schema (Phase E4).

Four judges answer the same question -- *which fault was the root cause of this flight?* -- over
the same frozen `RunBundle`, behind one signature. That uniformity is the experiment: B0 is the
deterministic floor, B1 the naive single LLM call, B2 repeated sampling at B3's measured spend,
and B3 the tool-using agent. A difference between two rows of the published table has to be
attributable to the judge and to nothing else, so:

  * a judge reads NOTHING outside the bundle it is handed -- no live link, no filesystem, no
    second data source;
  * every judge reports its own cost (`tokens_*`, `calls`, `wall_ms`) in the same units, because
    an accuracy comparison at unequal spend measures spend;
  * `bundle_id` travels inside the verdict, so a result can never be silently re-attributed to a
    different flight than the one it was made against.

`Verdict.root_cause = None` is a real answer, not a missing one. It means "a clean system stays
quiet", which is the correct answer for the null and wind scenarios and the thing a hallucinating
judge gets wrong.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from sentinel.bundle import RunBundle

if TYPE_CHECKING:  # avoids a circular import; Budget arrives in step 3
    from sentinel.budget import Budget


class Citation(BaseModel):
    """One pointer into the bundle supporting a verdict.

    `t` is checked against the captured window by the scorer. A citation to a moment the flight
    never observed is a fabrication however plausible the value looks -- spec section 11's
    fabrication probe, applied to the judge instead of to the advisory.
    """

    metric: str                    # a signal/evidence metric name, or an incident type
    t: float
    value: float | None = None


class Verdict(BaseModel):
    """One judge's answer for one bundle under one prompt variant."""

    judge: str                     # "B0" | "B1" | "B2" | "B3"
    bundle_id: str
    prompt_variant: str = "-"      # "-" for B0: no prompt is involved

    root_cause: str | None = None  # None == "a clean system stays quiet"
    symptoms: list[str] = Field(default_factory=list)
    confidence: float | None = None
    rationale: str = ""
    citations: list[Citation] = Field(default_factory=list)

    # Set when the spend ceiling tripped and the judge fell back to B0's answer. Never dropped
    # from the results: a judge that wins by exceeding its ceiling has not won, so the
    # degradation rate is published alongside accuracy.
    degraded: bool = False
    degraded_reason: str = ""

    tokens_in: int = 0
    tokens_out: int = 0
    calls: int = 0
    wall_ms: float = 0.0
    seed: int | None = None

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


@runtime_checkable
class Judge(Protocol):
    """Every judge, including the deterministic one, implements exactly this."""

    id: str

    def judge(self, bundle: RunBundle, budget: "Budget | None" = None,
              variant: str = "v1") -> Verdict:
        ...
