"""Regression tests for the fault-injection harness's parameter readback.

These cover one bug, because it was the expensive kind: it did not crash, it did not fail a
test, it produced three captured flights labelled "injection not applied" when the injection had
applied perfectly. A harness that lies about its own provenance is worse than one that breaks.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pymavlink", reason="harness scripts import pymavlink at module scope")

from scripts.r7_r8_scenarios import read_param  # noqa: E402


class FakeConn:
    """Minimal stand-in for a mavutil connection.

    `queued` is what is already sitting in the buffer before anything is requested -- the stale
    PARAM_VALUE messages ArduPilot broadcasts for an AP_Vector3f's siblings. `on_request` is what
    the autopilot replies with when actually asked.
    """

    target_system = 1
    target_component = 1

    def __init__(self, queued, on_request):
        self.queued = list(queued)
        self.on_request = list(on_request)
        self.requested: list[str] = []
        self.mav = self

    # -- the two mav methods read_param touches -------------------------------------------
    def param_request_read_send(self, sysid, compid, name, index):
        self.requested.append(name.decode() if isinstance(name, bytes) else name)
        self.queued.extend(self.on_request)

    def recv_match(self, type=None, blocking=False, timeout=None):
        return self.queued.pop(0) if self.queued else None


class Msg:
    def __init__(self, name, value):
        self.param_id = name
        self.param_value = value


def test_drains_stale_sibling_broadcast_before_reading():
    """The actual bug: writing SIM_MAG1_OFS_X queues a stale Y, and Y is what we read next.

    Undrained, the read returned the stale 13.0 (SITL.h:156's default) and the harness recorded
    the injection as not applied. Drained, it must return the value the autopilot actually holds.
    """
    conn = FakeConn(
        queued=[Msg("SIM_MAG1_OFS_Y", 13.0), Msg("SIM_MAG1_OFS_Z", -18.0)],
        on_request=[Msg("SIM_MAG1_OFS_Y", 400.0)],
    )
    assert read_param(conn, "SIM_MAG1_OFS_Y") == 400.0


def test_ignores_other_params_in_the_reply_stream():
    conn = FakeConn(
        queued=[],
        on_request=[Msg("SIM_WIND_SPD", 7.0), Msg("SIM_ACC1_RND", 90.0)],
    )
    assert read_param(conn, "SIM_ACC1_RND") == 90.0


def test_tolerates_bytes_param_ids_and_null_padding():
    conn = FakeConn(queued=[], on_request=[Msg(b"SIM_ACC1_RND\x00\x00\x00\x00", 70.0)])
    assert read_param(conn, "SIM_ACC1_RND") == 70.0


def test_returns_none_when_the_parameter_never_arrives():
    """A missing readback must stay None so `applied` goes False -- not silently pass."""
    conn = FakeConn(queued=[], on_request=[])
    assert read_param(conn, "SIM_ACC1_RND", timeout=0.2) is None


# --- ambiguous-pair "which symptom may lead" ------------------------------------------------

from scripts.r7_r8_scenarios import SCENARIOS, _leading  # noqa: E402


def test_leading_accepts_a_single_symptom():
    """Pair A pins one exact symptom; that behaviour must not change."""
    assert _leading({"first_advisory": "ekf_inconsistency"}) == ("ekf_inconsistency",)


def test_leading_accepts_several_symptoms():
    assert _leading({"first_advisory": ["a", "b"]}) == ("a", "b")


def test_every_declared_leader_is_also_a_declared_symptom():
    """A leader that is not in `symptoms` would be scored as an undeclared advisory.

    Catches the copy-paste error where a pair declares a first_advisory the cascade list does
    not permit -- the run would pass the ambiguity check and flag UNDECLARED at the same time.
    """
    for name, cfg in SCENARIOS.items():
        if "first_advisory" not in cfg:
            continue
        for leader in _leading(cfg):
            assert leader in cfg["symptoms"], f"{name}: {leader} leads but is not a symptom"


def test_no_pair_declares_its_root_cause_as_the_leader():
    """If the root cause may lead, the scenario is not ambiguous and measures nothing."""
    for name, cfg in SCENARIOS.items():
        if "first_advisory" not in cfg:
            continue
        assert cfg["expect"] not in _leading(cfg), f"{name}: root cause declared as leader"
