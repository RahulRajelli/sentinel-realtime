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

import re

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

    # Do the measurements quoted in the PROSE exist in the flight? Reported by default and only
    # score-affecting under strict_rationale, because gating on it would change the meaning of
    # every accuracy number measured before it existed.
    rationale_grounded: bool = True
    ungrounded_quotes: int = 0

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


# Numbers in prose that are worth checking. A bare small integer ("two sensors", "8 of 9") is
# ordinary English and flagging it would bury the real finding in noise. A decimal, or a number
# carrying a unit, is a claim about a measurement.
_MEASUREMENT = re.compile(
    r"(?<![\w.])(\d+\.\d+|\d+(?=\s*(?:s\b|sec|ms\b|V\b|Hz\b|deg\b|m/s|mGauss)))")
_UNIT_SUFFIX = re.compile(r"\s*(?:s\b|sec|ms\b|V\b|Hz\b|deg\b|m/s\S*|mGauss)")


def observable_numbers(bundle: RunBundle) -> set[float]:
    """Every number the flight actually recorded, which prose may legitimately quote."""
    out: set[float] = set()
    for c in bundle.cycles:
        out.add(round(c.t, 3))
        for inc in c.incidents:
            out.update({round(inc.t_start, 3), round(inc.t_end, 3)})
            for ev in inc.evidence:
                for v in (ev.value, ev.threshold):
                    if isinstance(v, (int, float)):
                        out.add(round(float(v), 3))
    for a in bundle.advisories:
        out.add(round(a.t, 3))
    for v in bundle.params.values():
        if isinstance(v, (int, float)):
            out.add(round(float(v), 3))
    if bundle.t_inject is not None:
        out.add(round(bundle.t_inject, 3))
    out.update({round(bundle.t_start, 3), round(bundle.t_end, 3)})
    return out


def check_rationale_grounding(bundle: RunBundle, verdict: Verdict) -> tuple[bool, list[str]]:
    """Do the measurements quoted in the RATIONALE exist in the flight?

    The gap this closes. `check_citations` validates the structured `citations` field, and
    nothing validated the prose. Measured 2026-08-14: gemini-2.5-flash wrote *"exceeded its
    threshold at 9.062s, leading to... the compass inconsistency at 10.016s"* while its
    structured citation was perfectly valid. The narrative a human actually reads was unchecked,
    and the narrative is where a confident wrong detail hides.

    Only decimals and unit-bearing numbers are checked. "8 of 9" and "two sensors" are English,
    not measurements, and flagging them would bury real findings in noise.

    Compared with a tolerance, and against ROUNDED observables, because a model quoting 9.06 for
    a recorded 9.062 is reading correctly rather than inventing.

    **Reported, not scored, by default.** Turning this into a pass/fail gate changes what every
    accuracy number in this repo means, and the honest order is to measure how often it fires
    before making it decide anything. `strict_rationale=True` on the scorer enables enforcement.
    """
    text = verdict.rationale or ""
    if not text.strip():
        return True, []

    observable = observable_numbers(bundle)
    problems: list[str] = []
    seen: set[float] = set()

    for raw in _MEASUREMENT.findall(text):
        try:
            val = float(raw)
        except ValueError:
            continue
        if val in seen:
            continue
        seen.add(val)
        tol = max(1e-3, abs(val) * 1e-2)
        if any(abs(val - o) <= tol for o in observable):
            continue
        # A rounded quote of a real number is honest reading, so check the rounded forms too.
        if any(abs(round(val, 1) - round(o, 1)) <= 1e-9 for o in observable):
            continue
        problems.append(f"rationale quotes {raw}, which the flight never recorded")

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


def score_verdict(bundle: RunBundle, verdict: Verdict,
                  strict_rationale: bool = False) -> ScoreRow:
    if verdict.bundle_id not in bundle.resolvable_identities():
        # Refuse rather than score. Silently grading a verdict against the wrong flight is the
        # one bug that would corrupt every downstream number without failing anything.
        #
        # Checked against every id this flight legitimately answers to, not just the current one:
        # a verdict written before the v1 -> v2 migration cites the id that was correct when it
        # was scored, and that citation is accurate history rather than a mismatch. The guard is
        # unchanged in strength -- a verdict for a DIFFERENT flight still matches nothing here.
        raise ValueError(
            f"verdict cites bundle {verdict.bundle_id}, scoring against {bundle.bundle_id} "
            f"(known also as {sorted(bundle.legacy_identities())})")

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

    grounded, ground_problems = check_rationale_grounding(bundle, verdict)
    notes.extend(ground_problems)

    # A null-expected scenario needs no citation: "nothing was wrong" has nothing to point at.
    needs_citation = predicted is not None
    has_citation = len(verdict.citations) > 0
    if needs_citation and not has_citation:
        notes.append("non-null verdict with no citation")

    score = 1.0 if (correct and citations_ok and (not needs_citation or has_citation)
                    and (grounded or not strict_rationale)) else 0.0

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
        rationale_grounded=grounded,
        ungrounded_quotes=len(ground_problems),
        score=score,
        degraded=verdict.degraded,
        tokens=verdict.tokens,
        wall_ms=verdict.wall_ms,
        attribution=None if score == 1.0 else attribute(bundle, verdict, citations_ok),
        notes=notes,
    )


def _identity_index(bundles: list[RunBundle]) -> tuple[dict[str, RunBundle],
                                                       dict[str, RunBundle],
                                                       set[str]]:
    """Build (current-id index, legacy-alias index, ambiguous aliases).

    Two levels, never one flat dict, because they carry different guarantees. A current
    `bundle_id` is the identity this build computes and must be unique. A legacy id is a
    historical name for the same flight, kept resolvable so that verdicts written before the
    v1 -> v2 migration still join -- but it is weaker evidence, so it never shadows a current id
    and an ambiguous one is refused rather than guessed at.
    """
    primary: dict[str, RunBundle] = {}
    for b in bundles:
        clash = primary.get(b.bundle_id)
        if clash is not None and clash is not b:
            raise ValueError(
                f"two bundles share bundle_id {b.bundle_id} ({clash.scenario} and {b.scenario}). "
                f"The archive cannot be scored until one is re-captured")
        primary[b.bundle_id] = b

    alias: dict[str, RunBundle] = {}
    ambiguous: set[str] = set()
    for b in bundles:
        for ident in b.resolvable_identities():
            if ident in primary:
                continue                      # a current id always wins
            held = alias.get(ident)
            if held is not None and held is not b:
                # Two flights that once hashed alike. Picking either would silently score a
                # verdict against the wrong flight, which is the exact failure this project
                # exists to catch. Refuse at use, and say which flights collided.
                ambiguous.add(ident)
                continue
            alias[ident] = b
    for ident in ambiguous:
        alias.pop(ident, None)
    return primary, alias, ambiguous


def score_all(bundles: list[RunBundle], verdicts: list[Verdict],
              strict_rationale: bool = False) -> list[ScoreRow]:
    """Score every verdict against the bundle it cites.

    Keyed by `bundle_id` rather than by list position, so a partial verdict set (one judge
    crashed on one bundle) scores what exists instead of silently misaligning everything after
    the gap.

    A verdict may cite a bundle by a legacy id (see `RunBundle.resolvable_identities`). That
    resolves, and the row is annotated so the substitution appears in the report rather than
    happening invisibly -- a join that quietly repairs itself is one you stop being able to audit.
    """
    primary, alias, ambiguous = _identity_index(bundles)
    rows: list[ScoreRow] = []
    for v in verdicts:
        bundle = primary.get(v.bundle_id)
        via_alias = False
        if bundle is None:
            if v.bundle_id in ambiguous:
                raise ValueError(
                    f"verdict cites {v.bundle_id}, which is a legacy id claimed by more than one "
                    f"bundle. Refusing to guess which flight was scored")
            bundle = alias.get(v.bundle_id)
            via_alias = bundle is not None
        if bundle is None:
            raise ValueError(
                f"verdict cites unknown bundle {v.bundle_id}. It matches no current id and no "
                f"legacy id of any loaded bundle -- check the --bundles directory covers the "
                f"flights this verdict set was scored against")
        row = score_verdict(bundle, v, strict_rationale=strict_rationale)
        if via_alias:
            row.notes.append(
                f"matched via legacy bundle_id {v.bundle_id} (now {bundle.bundle_id})")
        rows.append(row)
    return rows
