"""Step 5 acceptance: the B3 loop, end to end, with a scripted model. No tokens, no network.

The plan requires the degradation path to be exercised on purpose rather than merely reachable,
so every ceiling and every malformed-output path has a test that drives it.
"""

from __future__ import annotations

import json

import pytest

from flightdx.schema import Evidence, Incident
from sentinel.budget import Budget
from sentinel.bundle import AdvisoryRecord, CycleRecord, RunBundle
from sentinel.judges.agent import AgentJudge
from sentinel.judges.model import ModelResponse, ScriptedClient, answer, uses
from sentinel.judges.tools import FORBIDDEN_KEYS
from sentinel.score import score_verdict


@pytest.fixture()
def ambiguous() -> RunBundle:
    """The symptom (ekf) is detected a full cycle before the cause (compass)."""
    def inc(t, itype, metric, value, thr, sev="warning"):
        return Incident(t_start=t - 0.25, t_end=t, type=itype, severity=sev,
                        evidence=[Evidence(metric=metric, value=value, threshold=thr, unit="")])

    return RunBundle(
        scenario="compass_offset",
        expected_root_cause="compass_inconsistency",
        expected_symptoms=["ekf_inconsistency"],
        note="ambiguous pair A: the symptom leads the cause",
        t_inject=8.0, inject_verified=True,
        params={"COMPASS_OFS_X": 12.0},
        cycles=[
            CycleRecord(t=8.5, incidents=[]),
            CycleRecord(t=9.0, incidents=[inc(9.0, "ekf_inconsistency", "mag_variance", 2.4, 1.0)]),
            CycleRecord(t=10.0, incidents=[
                inc(10.0, "ekf_inconsistency", "mag_variance", 3.1, 1.0),
                inc(10.0, "compass_inconsistency", "mag_innovation", 6.2, 1.0, "critical")]),
        ],
        advisories=[
            AdvisoryRecord(t=9.0, type="ekf_inconsistency", severity="warning", reason="new"),
            AdvisoryRecord(t=10.0, type="compass_inconsistency", severity="critical", reason="new"),
        ],
    )


GOOD = json.dumps({
    "root_cause": "compass_inconsistency",
    "symptoms": ["ekf_inconsistency"],
    "confidence": 0.8,
    "rationale": "Mag innovation exceeded threshold; EKF variance is downstream of it.",
    "citations": [{"metric": "mag_innovation", "t": 10.0, "value": 6.2}],
})


# --- the happy path: tools, then a correct answer ---------------------------------------

def test_agent_uses_tools_then_answers_and_scores(ambiguous):
    client = ScriptedClient([
        uses("list_advisories"),
        uses("ordering", type_a="ekf_inconsistency", type_b="compass_inconsistency"),
        uses("detector_evidence", incident_type="compass_inconsistency"),
        answer(GOOD),
    ])
    v = AgentJudge(client).judge(ambiguous, Budget(), "v1")

    assert v.judge == "B3" and v.degraded is False
    assert v.root_cause == "compass_inconsistency"
    assert v.calls == 4 and v.tokens > 0 and v.wall_ms > 0
    assert score_verdict(ambiguous, v).score == 1.0


def test_the_agent_can_beat_b0_on_an_ambiguous_flight(ambiguous):
    """The reason B3 exists. B0 takes the first advisory and lands on the symptom."""
    from sentinel.judges.deterministic import DeterministicJudge
    b0 = DeterministicJudge().judge(ambiguous)
    b3 = AgentJudge(ScriptedClient([answer(GOOD)])).judge(ambiguous, Budget(), "v1")

    assert b0.root_cause == "ekf_inconsistency"          # the symptom
    assert score_verdict(ambiguous, b0).score == 0.0
    assert score_verdict(ambiguous, b0).named_symptom_as_root is True
    assert score_verdict(ambiguous, b3).score == 1.0     # the cause


# --- the label must be unreachable -------------------------------------------------------

def test_ground_truth_never_reaches_the_model(ambiguous):
    client = ScriptedClient([uses("list_advisories"),
                             uses("detector_evidence", incident_type="ekf_inconsistency"),
                             answer(GOOD)])
    AgentJudge(client).judge(ambiguous, Budget(), "v3")

    seen = client.everything_the_model_saw
    for key in FORBIDDEN_KEYS:
        assert key not in seen
    assert ambiguous.note not in seen
    assert ambiguous.scenario not in seen


def test_no_prompt_variant_names_a_fault_type(ambiguous):
    """A prompt listing candidate answers would leak the label more subtly than a field."""
    from sentinel.judges.prompts import VARIANTS
    for name, text in VARIANTS.items():
        assert "compass" not in text.lower(), name
        assert "vibration" not in text.lower(), name
        assert "ekf" not in text.lower(), name


# --- degradation: driven on purpose -------------------------------------------------------

def test_token_ceiling_degrades_to_b0_and_says_so(ambiguous):
    client = ScriptedClient([uses("list_advisories", tokens_in=900, tokens_out=200),
                             answer(GOOD)])
    v = AgentJudge(client).judge(ambiguous, Budget(max_tokens=1000), "v1")

    assert v.degraded is True and "token ceiling" in v.degraded_reason
    assert v.judge == "B3", "a degraded run must stay B3, not be relabelled B0"
    assert v.root_cause == "ekf_inconsistency"           # B0's answer, the symptom
    assert score_verdict(ambiguous, v).attribution == "harness"


def test_call_ceiling_degrades(ambiguous):
    client = ScriptedClient([uses("list_advisories", tokens_in=1, tokens_out=1)])
    v = AgentJudge(client).judge(ambiguous, Budget(max_calls=3, max_tokens=10**6), "v1")
    assert v.degraded and "call ceiling" in v.degraded_reason


def test_a_runaway_tool_loop_terminates(ambiguous):
    """ScriptedClient repeats its last response forever; max_turns must still end the run."""
    client = ScriptedClient([uses("list_advisories", tokens_in=1, tokens_out=1)])
    v = AgentJudge(client, max_turns=4).judge(ambiguous, Budget(max_tokens=10**9,
                                                                max_calls=10**9), "v1")
    assert v.degraded and "within 4 turns" in v.degraded_reason


def test_unparseable_output_retries_then_degrades(ambiguous):
    client = ScriptedClient([answer("I think it was the compass, honestly.")])
    v = AgentJudge(client).judge(ambiguous, Budget(), "v1")
    assert v.degraded and "unparseable" in v.degraded_reason
    assert client.calls == 3, "1 attempt + MAX_PARSE_RETRIES=2 retries, then degrade"


def test_recovers_when_the_retry_produces_valid_json(ambiguous):
    client = ScriptedClient([answer("no json here"), answer(GOOD)])
    v = AgentJudge(client).judge(ambiguous, Budget(), "v1")
    assert v.degraded is False and v.root_cause == "compass_inconsistency"


# --- output handling ----------------------------------------------------------------------

def test_json_wrapped_in_prose_or_fences_is_accepted(ambiguous):
    wrapped = f"Here is my analysis:\n```json\n{GOOD}\n```\nHope that helps."
    v = AgentJudge(ScriptedClient([answer(wrapped)])).judge(ambiguous, Budget(), "v1")
    assert v.root_cause == "compass_inconsistency"


@pytest.mark.parametrize("null_form", ["null", '"none"', '"N/A"', '""'])
def test_clean_flight_answers_are_not_read_as_a_fault(ambiguous, null_form):
    """A model writing "none" for no-fault must not be scored as hallucinating one."""
    payload = f'{{"root_cause": {null_form}, "symptoms": [], "citations": []}}'
    v = AgentJudge(ScriptedClient([answer(payload)])).judge(ambiguous, Budget(), "v1")
    assert v.root_cause is None


def test_malformed_citations_are_dropped_not_repaired(ambiguous):
    """Inventing a timestamp to make a citation parse would manufacture the evidence the
    scorer exists to check."""
    payload = json.dumps({
        "root_cause": "compass_inconsistency",
        "citations": [{"metric": "mag_innovation"},                    # no t
                      {"metric": "mag_innovation", "t": "later"},      # unparseable t
                      {"metric": "mag_innovation", "t": 10.0}],        # the only good one
    })
    v = AgentJudge(ScriptedClient([answer(payload)])).judge(ambiguous, Budget(), "v1")
    assert len(v.citations) == 1 and v.citations[0].t == 10.0


def test_a_confident_answer_citing_nothing_still_scores_zero(ambiguous):
    payload = json.dumps({"root_cause": "compass_inconsistency", "confidence": 0.99,
                          "citations": []})
    v = AgentJudge(ScriptedClient([answer(payload)])).judge(ambiguous, Budget(), "v1")
    row = score_verdict(ambiguous, v)
    assert row.score == 0.0 and row.attribution == "harness"


def test_tool_errors_do_not_end_the_run(ambiguous):
    client = ScriptedClient([uses("nonexistent_tool"),
                             uses("get_param", name="NOPE"),
                             answer(GOOD)])
    v = AgentJudge(client).judge(ambiguous, Budget(), "v1")
    assert v.degraded is False and v.root_cause == "compass_inconsistency"


def test_answering_with_no_tool_calls_is_allowed(ambiguous):
    """Forcing tool use would measure compliance, not judgement."""
    v = AgentJudge(ScriptedClient([answer(GOOD)])).judge(ambiguous, Budget(), "v1")
    assert v.calls == 1 and score_verdict(ambiguous, v).score == 1.0


def test_every_variant_runs_and_is_recorded(ambiguous):
    for variant in ("v1", "v2", "v3"):
        v = AgentJudge(ScriptedClient([answer(GOOD)])).judge(ambiguous, Budget(), variant)
        assert v.prompt_variant == variant


def test_unknown_variant_refuses(ambiguous):
    with pytest.raises(KeyError):
        AgentJudge(ScriptedClient([answer(GOOD)])).judge(ambiguous, Budget(), "v9")
