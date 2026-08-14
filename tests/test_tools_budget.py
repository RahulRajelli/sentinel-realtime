"""Step 4 acceptance: the tool surface and the spend ceiling, with no LLM in the loop.

The most important test in this file is `test_ground_truth_never_leaks`. A judge that could read
`expected_root_cause` would score 100% while measuring nothing, and every accuracy number in the
published table would be wrong with nothing failing.
"""

from __future__ import annotations

import json

import pytest

from flightdx.schema import Evidence, Incident
from sentinel.budget import Budget, k_for_budget, spend_match
from sentinel.bundle import AdvisoryRecord, CycleRecord, RunBundle
from sentinel.judges.tools import FORBIDDEN_KEYS, BundleTools


# --- an ambiguous flight: the symptom is detected a cycle BEFORE the cause -------------

@pytest.fixture()
def ambiguous() -> RunBundle:
    """compass_offset, in miniature: ekf_inconsistency at t=9.0, compass at t=10.0."""
    def inc(t, itype, metric, value, thr, sev="warning"):
        return Incident(t_start=t - 0.25, t_end=t, type=itype, severity=sev,
                        evidence=[Evidence(metric=metric, value=value, threshold=thr, unit="")])

    return RunBundle(
        scenario="compass_offset",
        expected_root_cause="compass_inconsistency",
        expected_symptoms=["ekf_inconsistency"],
        note="ambiguous pair A",
        t_inject=8.0,
        inject_verified=True,
        params={"COMPASS_OFS_X": 12.0, "MOT_PWM_MAX": 1900.0},
        cycles=[
            CycleRecord(t=8.5, incidents=[]),
            CycleRecord(t=9.0, incidents=[inc(9.0, "ekf_inconsistency", "mag_variance", 2.4, 1.0)]),
            CycleRecord(t=10.0, incidents=[
                inc(10.0, "ekf_inconsistency", "mag_variance", 3.1, 1.0),
                inc(10.0, "compass_inconsistency", "mag_innovation", 6.2, 1.0, "critical"),
            ]),
        ],
        advisories=[
            AdvisoryRecord(t=9.0, type="ekf_inconsistency", severity="warning", reason="new"),
            AdvisoryRecord(t=10.0, type="compass_inconsistency", severity="critical", reason="new"),
        ],
    )


# --- the leak test ---------------------------------------------------------------------

def test_ground_truth_never_leaks(ambiguous):
    """Nothing a judge can see may contain the answer."""
    tools = BundleTools(ambiguous)
    visible = json.dumps({
        "summary": tools.summarize(),
        "advisories": tools.list_advisories(),
        "ordering": tools.ordering("ekf_inconsistency", "compass_inconsistency"),
        "evidence": tools.detector_evidence("compass_inconsistency"),
        "window": tools.signal_window("mag_variance", 0.0, 99.0),
        "param": tools.get_param("COMPASS_OFS_X"),
    })
    for key in FORBIDDEN_KEYS:
        assert key not in visible, f"{key} reachable through the tool surface"

    # Checked by VALUE, not by key name. The bundle's note and scenario name both describe the
    # answer ("ambiguous pair A", "compass_offset"), and either would hand the judge the label
    # without any field called `expected_*` ever appearing.
    assert ambiguous.note not in visible, "the scenario note leaks the design of the test"
    assert ambiguous.scenario not in visible, "the scenario name names the root cause"

    # `compass_inconsistency` IS legitimately visible -- a detector really did raise it. What
    # must not be visible is which of the raised types is LABELLED the root cause.
    assert "compass_inconsistency" in visible


def test_summarize_is_an_allow_list(ambiguous):
    """A field added to RunBundle must be opted IN, so a future label cannot leak by omission."""
    allowed = {"flight_window_s", "cycles", "advisories", "detected_incident_types",
               "available_metrics", "params_hash", "params_captured"}
    assert set(BundleTools(ambiguous).summarize()) == allowed


# --- ordering: the question the whole agent turns on ------------------------------------

def test_ordering_reports_the_symptom_first(ambiguous):
    r = BundleTools(ambiguous).ordering("compass_inconsistency", "ekf_inconsistency")
    assert r["first"] == "ekf_inconsistency"
    assert r["delta_s"] == 1.0


def test_ordering_uses_incident_time_not_advisory_order(ambiguous):
    """Two incidents in one cycle are ordered by the gate via DETECTORS list order, which is
    arbitrary. `ordering` must report the physical timeline instead."""
    r = BundleTools(ambiguous).ordering("ekf_inconsistency", "compass_inconsistency")
    assert r["first"] == "ekf_inconsistency"


def test_ordering_same_cycle_refuses_to_invent_a_winner(ambiguous):
    b = ambiguous.model_copy(deep=True)
    b.cycles = [b.cycles[-1]]          # both types first appear in the same cycle
    r = BundleTools(b).ordering("ekf_inconsistency", "compass_inconsistency")
    assert r["first"] is None and r["delta_s"] == 0.0
    assert "cannot separate them" in r["note"]


def test_ordering_of_an_undetected_type(ambiguous):
    r = BundleTools(ambiguous).ordering("ekf_inconsistency", "gps_fix_loss")
    assert r["first"] == "ekf_inconsistency" and "never detected" in r["note"]


# --- errors are data, never exceptions --------------------------------------------------

@pytest.mark.parametrize("name,args", [
    ("detector_evidence", {"incident_type": "battery_voltage_sag"}),
    ("signal_window", {"metric": "nonexistent", "t0": 0.0, "t1": 99.0}),
    ("signal_window", {"metric": "mag_variance", "t0": 50.0, "t1": 10.0}),
    ("get_param", {"name": "NOT_A_PARAM"}),
    ("ordering", {"type_a": "a", "type_b": "b"}),
])
def test_failed_lookups_return_error_dicts(ambiguous, name, args):
    out = BundleTools(ambiguous).call(name, args)
    assert isinstance(out, dict) and "error" in out


def test_unknown_tool_and_bad_arguments_return_data(ambiguous):
    tools = BundleTools(ambiguous)
    assert "error" in tools.call("delete_everything", {})
    assert "available" in tools.call("delete_everything", {})
    assert "error" in tools.call("ordering", {"type_a": "x"})          # missing type_b
    assert "error" in tools.call("get_param", {"wrong_kwarg": "x"})


def test_no_tool_can_mutate_the_bundle(ambiguous):
    before = ambiguous.bundle_id
    tools = BundleTools(ambiguous)
    for spec in BundleTools.SPECS:
        tools.call(spec["name"], {"type_a": "a", "type_b": "b", "incident_type": "x",
                                  "metric": "m", "t0": 0.0, "t1": 1.0, "name": "P"})
    assert ambiguous.bundle_id == before


def test_signal_window_never_returns_a_raw_series(ambiguous):
    out = BundleTools(ambiguous).signal_window("mag_variance", 0.0, 99.0)
    assert out["n"] == 2 and out["max"] == 3.1
    assert not any(isinstance(v, list) for v in out.values())


# --- budget ------------------------------------------------------------------------------

def test_budget_trips_on_tokens_then_calls_then_wall():
    b = Budget(max_tokens=100, max_calls=99, max_wall_s=99).start()
    assert not b.tripped
    b.charge(60, 50)
    assert b.tripped and "token ceiling" in b.reason

    c = Budget(max_tokens=10_000, max_calls=2, max_wall_s=99).start()
    c.charge(1, 1); c.charge(1, 1)
    assert c.tripped and "call ceiling" in c.reason


def test_trip_reason_is_stable_when_two_limits_blow_at_once():
    b = Budget(max_tokens=10, max_calls=1).start()
    b.charge(20, 20)
    assert b.reason.startswith("token ceiling")
    assert b.reason == b.reason      # not scheduling-dependent


def test_manual_trip_for_what_counters_cannot_see():
    b = Budget().start()
    b.trip("model returned unparseable JSON three times")
    assert b.tripped and "unparseable" in b.reason


def test_budget_never_raises():
    b = Budget(max_tokens=1).start()
    for _ in range(50):
        b.charge(100, 100)           # must not raise; degradation is the agent's decision
    assert b.tripped and b.calls == 50


def test_snapshot_is_the_verdict_cost_fields():
    b = Budget().start(); b.charge(120, 40)
    s = b.snapshot()
    assert s["tokens_in"] == 120 and s["tokens_out"] == 40 and s["calls"] == 1
    assert s["wall_ms"] >= 0.0


# --- matched-budget arithmetic: the thing B2 exists for -----------------------------------

def test_k_floors_so_the_baseline_cannot_outspend_the_agent():
    assert k_for_budget(1000, 300) == 3        # 3.33 -> 3, never 4
    assert k_for_budget(1000, 1500) == 1       # one sample already overspends; report it
    assert k_for_budget(1000, 0) == 1          # no divide-by-zero
    assert k_for_budget(10**9, 1) == 32        # capped


def test_spend_match_is_reported_not_asserted():
    assert spend_match(1000, 1050) == pytest.approx(0.05)
    assert spend_match(1000, 900) == pytest.approx(-0.10)
    assert spend_match(0, 500) == 0.0
