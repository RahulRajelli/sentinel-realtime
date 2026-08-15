"""Step 2 acceptance: B0 + the scorer reproduce today's R8 result, offline.

No SITL, no network, no LLM. Bundles are reconstructed from the committed `r8_results.json` so
the check is against the numbers that were actually measured, not against invented ones.

The reconstruction is deliberately faithful about the thing under test -- which advisories fired,
in what order, relative to injection -- and indifferent about everything else. B0 reads only
`bundle.advisories`, so that is what has to be right.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from flightdx.schema import Evidence, Incident
from sentinel.bundle import (
    AdvisoryRecord,
    CycleRecord,
    InjectedParam,
    RunBundle,
    RunMetrics,
)
from sentinel.judges import Citation, Verdict
from sentinel.judges.deterministic import DeterministicJudge
from sentinel.score import score_all, score_verdict

ROOT = Path(__file__).resolve().parent.parent
R8 = ROOT / "r8_results.json"

# Mirrors SCENARIOS in scripts/r7_r8_scenarios.py. Duplicated rather than imported because that
# module imports pymavlink at module scope, which the offline test suite must not require.
SYMPTOMS = {
    "null": [],
    "vibration": ["accel_clipping"],
    "gps_loss": ["ekf_inconsistency", "gps_high_hdop"],
    "wind": ["actuator_saturation", "control_oscillation"],
}

T_INJECT = 8.0
EVIDENCE_METRIC = {
    "vibration_excessive": "vibe_x",
    "accel_clipping": "clip_count",
    "gps_fix_loss": "gps_fix_type",
}


def synth_bundle(row: dict) -> RunBundle:
    """Rebuild a plausible bundle from one r8_results row.

    `first_advised` must come first in time and the rest of `all_advised` after it -- that
    ordering is the only thing B0 consumes, and getting it wrong would make this test pass for
    the wrong reason.
    """
    scenario = row["scenario"]
    first = row.get("first_advised")
    others = [t for t in row.get("all_advised", []) if t != first]
    ordered = ([first] if first else []) + others

    cycles: list[CycleRecord] = [
        CycleRecord(t=float(t), incidents=[], buffer_records=200 + 60 * t, detect_ms=0.4)
        for t in range(1, int(T_INJECT) + 1)
    ]
    advisories: list[AdvisoryRecord] = []

    for i, itype in enumerate(ordered):
        t = T_INJECT + 1.0 + i
        metric = EVIDENCE_METRIC.get(itype, itype)
        inc = Incident(
            t_start=t - 1.0, t_end=t, type=itype, severity="warning",
            evidence=[Evidence(metric=metric, value=61.0, threshold=30.0, unit="")],
        )
        cycles.append(CycleRecord(t=t, incidents=[inc], buffer_records=700 + 60 * i,
                                  detect_ms=1.2))
        advisories.append(AdvisoryRecord(t=t, type=itype, severity="warning",
                                         reason="new", pre_inject=False))

    injected = [
        InjectedParam(name=p.split("=")[0], value=float(p.split("=")[1]),
                      readback=float(p.split("=")[1]), applied=True)
        for p in row.get("injected", [])
    ]

    return RunBundle(
        scenario=scenario,
        expected_root_cause=row.get("expected"),
        expected_symptoms=SYMPTOMS.get(scenario, []),
        injection=injected,
        t_inject=T_INJECT if injected else None,
        inject_verified=row.get("inject_verified", True),
        params={"INS_ACCEL_FILTER": 20.0},
        cycles=cycles,
        advisories=advisories,
        metrics=RunMetrics(latency_s=row.get("latency_s"),
                           suppression=row.get("suppression", 0.0)),
    )


@pytest.fixture(scope="module")
def bundles() -> list[RunBundle]:
    rows = json.loads(R8.read_text())
    return [synth_bundle(r) for r in rows]


@pytest.fixture(scope="module")
def r8_rows() -> list[dict]:
    return json.loads(R8.read_text())


# --- the acceptance criterion ---------------------------------------------------------

def test_b0_reproduces_r8_pass_fail(bundles, r8_rows):
    """B0 + scorer must agree with the live harness's own pass/fail, offline."""
    judge = DeterministicJudge()
    for bundle, row in zip(bundles, r8_rows):
        verdict = judge.judge(bundle)
        score = score_verdict(bundle, verdict)
        assert score.correct is bool(row["ok"]), (
            f"{bundle.scenario}: harness said ok={row['ok']}, "
            f"B0 said {verdict.root_cause!r} vs expected {bundle.expected_root_cause!r}")


def test_b0_scores_perfectly_on_the_current_fault_set(bundles):
    """Documented on purpose: the deterministic floor is already 100% here.

    Consequence, and the reason this test is named the way it is -- on this scenario set no
    agent can beat B0. The best available outcome is a tie at higher cost. Any measurement of
    E4's value requires faults where the first advisory is NOT the root cause.
    """
    judge = DeterministicJudge()
    rows = score_all(bundles, [judge.judge(b) for b in bundles])
    assert sum(r.score for r in rows) == len(rows)


def test_b0_never_fails_its_own_citation_check(bundles):
    """B0 is the fallback every other judge degrades to, so it must not be capable of
    producing an unresolvable citation."""
    judge = DeterministicJudge()
    for bundle in bundles:
        row = score_verdict(bundle, judge.judge(bundle))
        assert row.citations_resolve, row.notes


# --- the failure modes the scorer exists to catch --------------------------------------

def _vib(bundles) -> RunBundle:
    return next(b for b in bundles if b.scenario == "vibration")


def test_symptom_named_as_root_scores_zero(bundles):
    b = _vib(bundles)
    v = Verdict(judge="B3", bundle_id=b.bundle_id, root_cause="accel_clipping",
                citations=[Citation(metric="clip_count", t=10.0)])
    row = score_verdict(b, v)
    assert row.score == 0.0
    assert row.named_symptom_as_root is True
    assert row.attribution == "model"


def test_hallucination_on_a_clean_flight_scores_zero(bundles):
    b = next(x for x in bundles if x.scenario == "null")
    v = Verdict(judge="B1", bundle_id=b.bundle_id, root_cause="vibration_excessive",
                citations=[Citation(metric="vibe_x", t=4.0)])
    row = score_verdict(b, v)
    assert row.score == 0.0 and row.hallucinated is True


def test_staying_quiet_on_a_clean_flight_is_correct(bundles):
    b = next(x for x in bundles if x.scenario == "wind")
    row = score_verdict(b, Verdict(judge="B1", bundle_id=b.bundle_id, root_cause=None))
    assert row.score == 1.0 and row.correct is True


def test_right_answer_with_a_fabricated_citation_scores_zero(bundles):
    """Being right while pointing at something that never happened is not being right."""
    b = _vib(bundles)
    v = Verdict(judge="B3", bundle_id=b.bundle_id, root_cause="vibration_excessive",
                citations=[Citation(metric="vibe_x", t=999.0)])
    row = score_verdict(b, v)
    assert row.score == 0.0
    assert row.citations_resolve is False
    assert row.attribution == "harness"


def test_right_answer_citing_an_invented_metric_scores_zero(bundles):
    b = _vib(bundles)
    v = Verdict(judge="B3", bundle_id=b.bundle_id, root_cause="vibration_excessive",
                citations=[Citation(metric="motor_temperature", t=9.0)])
    assert score_verdict(b, v).score == 0.0


def test_right_answer_with_no_citation_scores_zero(bundles):
    """Otherwise guessing is free."""
    b = _vib(bundles)
    v = Verdict(judge="B2", bundle_id=b.bundle_id, root_cause="vibration_excessive")
    row = score_verdict(b, v)
    assert row.score == 0.0 and row.attribution == "harness"


def test_degraded_verdict_is_attributed_to_harness(bundles):
    b = _vib(bundles)
    v = Verdict(judge="B3", bundle_id=b.bundle_id, root_cause="accel_clipping",
                citations=[Citation(metric="clip_count", t=10.0)],
                degraded=True, degraded_reason="token ceiling")
    assert score_verdict(b, v).attribution == "harness"


def test_unverified_injection_is_attributed_to_environment(bundles):
    """If the fault never applied, no judge could have been right. Blaming the model there
    would be a measurement error, not a finding."""
    b = _vib(bundles).model_copy(deep=True)
    b.inject_verified = False
    v = Verdict(judge="B3", bundle_id=b.bundle_id, root_cause="accel_clipping",
                citations=[Citation(metric="clip_count", t=10.0)])
    assert score_verdict(b, v).attribution == "environment"


def test_crashed_detector_is_attributed_to_environment(bundles):
    b = _vib(bundles).model_copy(deep=True)
    b.cycles[-1].per_detector_ms["vibration"] = -1.0
    v = Verdict(judge="B3", bundle_id=b.bundle_id, root_cause="accel_clipping",
                citations=[Citation(metric="clip_count", t=10.0)])
    assert score_verdict(b, v).attribution == "environment"


def test_scoring_a_verdict_against_the_wrong_bundle_refuses(bundles):
    """The one bug that would corrupt every downstream number without failing anything."""
    a, b = bundles[0], bundles[1]
    v = Verdict(judge="B0", bundle_id=a.bundle_id, root_cause=None)
    with pytest.raises(ValueError, match="cites bundle"):
        score_verdict(b, v)


# --- bundle identity must survive a re-run on the same data ----------------------------------

def test_bundle_id_ignores_wall_clock_timings():
    """Found 2026-08-14: replaying one log twice produced two different ids, because the hash
    included per-cycle detect_ms. Identity must fingerprint the flight, not the host."""
    def make(detect_ms: float) -> RunBundle:
        return RunBundle(
            scenario="replay:x", expected_root_cause=None,
            cycles=[CycleRecord(t=1.0, incidents=[], detect_ms=detect_ms,
                                build_ms=detect_ms / 2, messages_in=int(detect_ms),
                                per_detector_ms={"vibration": detect_ms})],
            advisories=[], params={"P": 1.0},
        )
    assert make(0.4).bundle_id == make(97.3).bundle_id


def test_bundle_id_still_changes_when_the_flight_changes():
    """The corollary: loosening identity must not make it blind."""
    base = RunBundle(scenario="replay:x", cycles=[CycleRecord(t=1.0)], params={"P": 1.0})
    moved = RunBundle(scenario="replay:x", cycles=[CycleRecord(t=2.0)], params={"P": 1.0})
    other = RunBundle(scenario="replay:x", cycles=[CycleRecord(t=1.0)], params={"P": 2.0})
    assert base.bundle_id != moved.bundle_id
    assert base.bundle_id != other.bundle_id


# --- citation anchors -----------------------------------------------------------------------
#
# Added 2026-08-15 with the value anchor. Before it, `Citation.t` was mandatory, so a judge
# reading `evidence_untimed` -- which removes every timestamp by design -- could not produce a
# valid citation at all. Measured: 2 of 9 agent verdicts per run named the CORRECT root cause and
# scored zero for it, in all five repeats.
#
# The risk in relaxing a validation rule is that it stops validating. These tests exist to prove
# it still does: a fabricated value must fail exactly as a fabricated timestamp does.

from sentinel.score import check_citations, recorded_values  # noqa: E402


def _cited_bundle() -> RunBundle:
    """One flight with a single known evidence value: mag_variance = 2.619 at t=10.0."""
    inc = Incident(t_start=9.75, t_end=10.0, type="compass_inconsistency", severity="warning",
                   evidence=[Evidence(metric="EKF_Magnetometer_Variance", value=2.619,
                                      threshold=1.0, unit="ratio")])
    return RunBundle(
        scenario="compass_offset", expected_root_cause="compass_inconsistency",
        t_inject=8.0, inject_verified=True,
        cycles=[CycleRecord(t=9.0, incidents=[]), CycleRecord(t=10.0, incidents=[inc])],
        advisories=[AdvisoryRecord(t=10.0, type="compass_inconsistency",
                                   severity="warning", reason="new")],
    )


def _verdict(citations) -> Verdict:
    return Verdict(judge="B3", bundle_id="x", root_cause="compass_inconsistency",
                   citations=citations)


def test_value_anchored_citation_is_accepted():
    """The whole point: no timestamp, but the value is real."""
    ok, problems = check_citations(_cited_bundle(), _verdict(
        [Citation(metric="EKF_Magnetometer_Variance", value=2.619)]))
    assert ok, problems


def test_fabricated_value_is_rejected():
    """The anti-relaxation test. An invented number must fail like an invented timestamp."""
    ok, problems = check_citations(_cited_bundle(), _verdict(
        [Citation(metric="EKF_Magnetometer_Variance", value=99.9)]))
    assert not ok
    assert "matches no recorded value" in problems[0]


def test_citation_with_neither_anchor_is_rejected():
    """Newly expressible, and newly the main risk: a bare metric name points at nothing."""
    ok, problems = check_citations(_cited_bundle(), _verdict(
        [Citation(metric="EKF_Magnetometer_Variance")]))
    assert not ok
    assert "neither a timestamp nor a value" in problems[0]


def test_temporal_citation_still_works():
    ok, _ = check_citations(_cited_bundle(), _verdict(
        [Citation(metric="EKF_Magnetometer_Variance", t=10.0)]))
    assert ok


def test_timestamp_outside_the_window_still_fails():
    ok, problems = check_citations(_cited_bundle(), _verdict(
        [Citation(metric="EKF_Magnetometer_Variance", t=999.0)]))
    assert not ok and "outside window" in problems[0]


def test_unknown_metric_fails_whichever_anchor_is_used():
    for cite in (Citation(metric="not_a_metric", t=10.0),
                 Citation(metric="not_a_metric", value=2.619)):
        ok, problems = check_citations(_cited_bundle(), _verdict([cite]))
        assert not ok and "never appears" in problems[0]


def test_value_anchor_tolerates_a_json_round_trip():
    """Both sides have been through JSON; exact float equality would reject honest citations."""
    ok, _ = check_citations(_cited_bundle(), _verdict(
        [Citation(metric="EKF_Magnetometer_Variance", value=2.6190001)]))
    assert ok


def test_recorded_values_reads_the_flight():
    assert recorded_values(_cited_bundle(), "EKF_Magnetometer_Variance") == [2.619]
    assert recorded_values(_cited_bundle(), "nope") == []
