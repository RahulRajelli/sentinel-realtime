"""Root-cause-only scorer (Phase E4).

The spec's rule, unchanged since `gate.py:21` deferred to it: **credit goes only to the injected
root cause.** Naming a symptom of the injected fault scores zero, not partial credit -- that
confusion is the specific failure E4 exists to measure, and a scorer that gave it half a mark
would hide the only interesting result.

This file is the authority. No LLM decides anything here. A separate rubric grader
(`judges/grader.py`, step 7) exists solely as a *second rater* so Cohen's kappa can measure
whether LLM grading agrees with this code -- *Reliability without Validity* found a 33-41 point
judge validation gap that raw agreement hides. When they disagree, this file is right.

Three strictnesses, each deliberate:

  1. **A symptom scores zero.** See above.
  2. **A correct answer with an unresolvable citation scores zero.** Spec section 11's
     fabrication probe: a citation that does not resolve fails the run. Being right by luck,
     while pointing at something that never happened, is not being right.
  3. **A non-null verdict must cite at least one thing.** Otherwise guessing is free, and a
     judge that always answers "vibration_excessive" would score respectably on any fault set
     with vibration in it.

Ground truth lives in the bundle (`expected_root_cause`, `expected_symptoms`), not here, so a
bundle handed to someone else is self-contained and independently scoreable.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sentinel.bundle import RunBundle
from sentinel.judges import Verdict

# Buckets from *Model or Harness*. Assigned by rule below, never by opinion -- an attribution
# that depends on the reviewer's mood is not a measurement.
ATTRIBUTION = ("model", "harness", "environment")


class ScoreRow(BaseModel):
    """One (bundle, judge, variant) outcome. The unit the statistics aggregate over."""

    bundle_id: str
    scenario: str
    judge: str
    variant: str

    expected: str | None = None
    predicted: str | None = None

    correct: bool = False
    named_symptom_as_root: bool = False   # the failure mode E4 exists to measure
    hallucinated: bool = False            # named a fault on a scenario that injected nothing
    missed: bool = False                  # stayed silent when a fault was injected

    citations_resolve: bool = True
    citation_count: int = 0

    score: float = 0.0
    degraded: bool = False
    tokens: int = 0
    wall_ms: float = 0.0

    attribution: str | None = None        # set only when score == 0.0
    notes: list[str] = Field(default_factory=list)


def known_metrics(bundle: RunBundle) -> set[str]:
    """Every metric name a citation may legitimately refer to.

    Both evidence metrics and incident types are accepted: a judge citing `vibe_x` at t=9.0 and
    one citing `vibration_excessive` at t=9.0 are pointing at the same observation at different
    levels of detail, and neither is a fabrication.
    """
    names: set[str] = set()
    for cycle in bundle.cycles:
        for inc in cycle.incidents:
            names.add(inc.type)
            names.update(ev.metric for ev in inc.evidence)
    return names


def recorded_values(bundle: RunBundle, metric: str) -> list[float]:
    """Every value recorded for one metric, across the flight."""
    return [ev.value for c in bundle.cycles for inc in c.incidents for ev in inc.evidence
            if ev.metric == metric and isinstance(ev.value, (int, float))]


def check_citations(bundle: RunBundle, verdict: Verdict) -> tuple[bool, list[str]]:
    """Every citation must name something the flight observed, anchored to something real.

    Two anchors, and a citation needs at least one:

    * `t` -- must fall inside the captured window (the original rule, unchanged);
    * `value` -- must match a value actually recorded for that metric.

    The value anchor exists because `evidence_untimed` removes timestamps by design, so a judge
    using it has no `t` to cite and was scoring zero on correct answers. It is NOT a softer rule:
    an invented number fails just as an invented timestamp does. Compared with a tolerance
    because both sides have been through JSON and a float that survives a round trip is not
    guaranteed to compare equal.

    A citation with neither anchor is rejected. That is the case that used to be impossible to
    express and is now the one real risk: a judge could otherwise "cite" a bare metric name and
    have it pass.
    """
    problems: list[str] = []
    valid = known_metrics(bundle)

    for c in verdict.citations:
        if c.metric not in valid:
            problems.append(f"citation metric {c.metric!r} never appears in this bundle")
            continue

        if not c.anchored:
            problems.append(
                f"citation {c.metric} carries neither a timestamp nor a value; "
                f"a metric name alone points at nothing checkable")
            continue

        if c.t is not None and not bundle.contains_time(c.t):
            problems.append(
                f"citation {c.metric}@{c.t} outside window [{bundle.t_start}, {bundle.t_end}]")

        if c.t is None and c.value is not None:
            seen = recorded_values(bundle, c.metric)
            tol = max(1e-6, abs(c.value) * 1e-3)
            if not any(abs(v - c.value) <= tol for v in seen):
                problems.append(
                    f"citation {c.metric}={c.value} matches no recorded value for that metric "
                    f"(observed range {min(seen):g}..{max(seen):g})" if seen else
                    f"citation {c.metric}={c.value} but no values were recorded for that metric")

    return (not problems), problems


def attribute(bundle: RunBundle, verdict: Verdict, citations_ok: bool) -> str:
    """Why did this verdict fail? Assigned by rule, in priority order.

    Environment first: if the fault never actually applied, or a detector crashed, then no judge
    could have got it right and blaming the model would be a measurement error. Harness second:
    the judge was prevented from answering properly. Model last, and only by elimination -- it
    had the evidence and the budget and still judged wrong.
    """
    if not bundle.inject_verified and bundle.expected_root_cause is not None:
        return "environment"
    if any(ms == -1.0 for c in bundle.cycles for ms in c.per_detector_ms.values()):
        return "environment"

    if verdict.degraded:
        return "harness"
    if not citations_ok:
        return "harness"
    if verdict.root_cause is not None and not verdict.citations:
        return "harness"

    return "model"


def score_verdict(bundle: RunBundle, verdict: Verdict) -> ScoreRow:
    if verdict.bundle_id != bundle.bundle_id:
        # Refuse rather than score. Silently grading a verdict against the wrong flight is the
        # one bug that would corrupt every downstream number without failing anything.
        raise ValueError(
            f"verdict cites bundle {verdict.bundle_id}, scoring against {bundle.bundle_id}")

    expected = bundle.expected_root_cause
    predicted = verdict.root_cause
    notes: list[str] = []

    correct = predicted == expected
    named_symptom = (expected is not None
                     and predicted is not None
                     and predicted in bundle.expected_symptoms)
    hallucinated = expected is None and predicted is not None
    missed = expected is not None and predicted is None

    citations_ok, problems = check_citations(bundle, verdict)
    notes.extend(problems)

    # A null-expected scenario needs no citation: "nothing was wrong" has nothing to point at.
    needs_citation = predicted is not None
    has_citation = len(verdict.citations) > 0
    if needs_citation and not has_citation:
        notes.append("non-null verdict with no citation")

    score = 1.0 if (correct and citations_ok and (not needs_citation or has_citation)) else 0.0

    if named_symptom:
        notes.append(f"named symptom {predicted!r} as root cause of {expected!r}")

    return ScoreRow(
        bundle_id=bundle.bundle_id,
        scenario=bundle.scenario,
        judge=verdict.judge,
        variant=verdict.prompt_variant,
        expected=expected,
        predicted=predicted,
        correct=correct,
        named_symptom_as_root=named_symptom,
        hallucinated=hallucinated,
        missed=missed,
        citations_resolve=citations_ok,
        citation_count=len(verdict.citations),
        score=score,
        degraded=verdict.degraded,
        tokens=verdict.tokens,
        wall_ms=verdict.wall_ms,
        attribution=None if score == 1.0 else attribute(bundle, verdict, citations_ok),
        notes=notes,
    )


def score_all(bundles: list[RunBundle], verdicts: list[Verdict]) -> list[ScoreRow]:
    """Score every verdict against the bundle it cites.

    Keyed by `bundle_id` rather than by list position, so a partial verdict set (one judge
    crashed on one bundle) scores what exists instead of silently misaligning everything after
    the gap.
    """
    by_id = {b.bundle_id: b for b in bundles}
    rows: list[ScoreRow] = []
    for v in verdicts:
        bundle = by_id.get(v.bundle_id)
        if bundle is None:
            raise ValueError(f"verdict cites unknown bundle {v.bundle_id}")
        rows.append(score_verdict(bundle, v))
    return rows
