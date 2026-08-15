"""Cross-flight durable memory (Phase E5), offline.

Until this existed the system's entire temporal context was a 120-second rolling buffer, so every
flight was judged as though it were the first that airframe had ever flown. These tests pin the
three properties that make the history usable as evidence rather than as decoration: it is
append-only, it never leaks the answer, and it excludes the flight under judgement from its own
history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from flightdx.schema import Evidence, Incident
from sentinel.bundle import AdvisoryRecord, CycleRecord, RunBundle
from sentinel.judges.tools import FORBIDDEN_KEYS, BundleTools
from sentinel.memory import FlightHistory

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _bundle(bundle_note: str, types: list[str], when: datetime,
            airframe: str = "hex-01", scenario: str = "compass_offset") -> RunBundle:
    inc = Incident(t_start=9.0, t_end=10.0, type=types[0] if types else "none",
                   severity="warning",
                   evidence=[Evidence(metric="m", value=2.0, threshold=1.0, unit="")])
    return RunBundle(
        scenario=scenario,
        note=bundle_note,
        airframe_id=airframe,
        created_utc=when.isoformat(timespec="seconds"),
        expected_root_cause="compass_inconsistency",
        expected_symptoms=["ekf_inconsistency"],
        t_inject=8.0, inject_verified=True,
        cycles=[CycleRecord(t=10.0, incidents=[inc])],
        advisories=[AdvisoryRecord(t=10.0 + i, type=t, severity="warning", reason="new")
                    for i, t in enumerate(types)],
    )


@pytest.fixture()
def store():
    """Repo-local temp file rather than pytest's `tmp_path`.

    This environment denies access to pytest's tmp root (WinError 5 on `pytest-of-<user>`), which
    would make every test in this file error at setup for a reason unrelated to what they test.
    A unique filename per test keeps them independent, and the file is removed afterwards.
    """
    d = Path(__file__).resolve().parent / ".tmp-memory"
    d.mkdir(exist_ok=True)
    path = d / f"flights-{uuid4().hex}.jsonl"
    yield FlightHistory(path)
    path.unlink(missing_ok=True)


def test_history_is_append_only(store):
    """A record that can be rewritten cannot be used as evidence."""
    store.record_flight(_bundle("a", ["ekf_inconsistency"], NOW - timedelta(days=3)), "hex-01")
    store.record_flight(_bundle("b", ["compass_inconsistency"], NOW - timedelta(days=2)), "hex-01")
    assert len(store.path.read_text(encoding="utf-8").strip().splitlines()) == 2
    store.record_flight(_bundle("c", ["ekf_inconsistency"], NOW - timedelta(days=1)), "hex-01")
    assert len(store.path.read_text(encoding="utf-8").strip().splitlines()) == 3


def test_recurrence_is_counted_across_flights(store):
    for d in (5, 4, 3):
        store.record_flight(
            _bundle(f"f{d}", ["ekf_inconsistency", "compass_inconsistency"],
                    NOW - timedelta(days=d)), "hex-01")
    out = store.prior_incidents("hex-01", now=NOW)
    assert out["flights_in_window"] == 3
    assert out["recurring"]["compass_inconsistency"]["flights_with_it"] == 3
    assert out["recurring"]["compass_inconsistency"]["of_flights"] == 3


def test_the_flight_under_judgement_is_excluded_from_its_own_history(store):
    """Otherwise every fault looks like a recurrence of itself."""
    b = _bundle("current", ["compass_inconsistency"], NOW)
    store.record_flight(b, "hex-01")
    out = store.prior_incidents("hex-01", exclude_bundle_id=b.bundle_id, now=NOW)
    assert out["flights_in_window"] == 0


def test_other_airframes_are_not_counted(store):
    store.record_flight(_bundle("mine", ["compass_inconsistency"], NOW - timedelta(days=1)),
                        "hex-01")
    store.record_flight(_bundle("theirs", ["compass_inconsistency"], NOW - timedelta(days=1)),
                        "quad-99")
    assert store.prior_incidents("quad-99", now=NOW)["flights_in_window"] == 1
    assert store.prior_incidents("hex-01", now=NOW)["flights_in_window"] == 1


def test_flights_outside_the_window_are_dropped(store):
    store.record_flight(_bundle("old", ["compass_inconsistency"], NOW - timedelta(days=400)),
                        "hex-01")
    assert store.prior_incidents("hex-01", within_days=30, now=NOW)["flights_in_window"] == 0
    assert store.prior_incidents("hex-01", within_days=500, now=NOW)["flights_in_window"] == 1


def test_empty_history_says_so_rather_than_implying_health(store):
    out = store.prior_incidents("never-flown", now=NOW)
    assert out["flights_in_window"] == 0
    assert "not evidence of a healthy airframe" in out["note"]


def test_a_corrupt_line_does_not_blind_the_whole_history(store):
    store.record_flight(_bundle("good", ["compass_inconsistency"], NOW - timedelta(days=1)),
                        "hex-01")
    with store.path.open("a", encoding="utf-8") as fh:
        fh.write("{not json\n")
    assert store.prior_incidents("hex-01", now=NOW)["flights_in_window"] == 1


def test_history_never_leaks_the_ground_truth(store):
    """The same leak tools.py guards within a flight, applied across flights."""
    import json
    store.record_flight(_bundle("x", ["compass_inconsistency"], NOW - timedelta(days=1)),
                        "hex-01")
    raw = store.path.read_text(encoding="utf-8")
    for key in FORBIDDEN_KEYS:
        assert key not in raw
    visible = json.dumps(store.prior_incidents("hex-01", now=NOW))
    for key in FORBIDDEN_KEYS:
        assert key not in visible
    # The scenario NAME describes the answer too, and it is not in the aggregate.
    assert "compass_offset" not in visible


def test_the_tool_reports_absence_instead_of_guessing(store):
    """No history attached, and no airframe id, are different states and both are stated."""
    b = _bundle("x", ["compass_inconsistency"], NOW, airframe="")
    assert "error" in BundleTools(b).prior_incidents()
    assert "no airframe_id" in BundleTools(b, history=store).prior_incidents()["error"]


def test_the_tool_returns_counts_when_wired(store):
    for d in (3, 2):
        store.record_flight(_bundle(f"f{d}", ["compass_inconsistency"], NOW - timedelta(days=d)),
                            "hex-01")
    current = _bundle("current", ["ekf_inconsistency"], NOW)
    out = BundleTools(current, history=store).prior_incidents()
    assert out["recurring"]["compass_inconsistency"]["flights_with_it"] == 2


def test_prior_incidents_is_opt_in_not_a_default_tool():
    """It reaches outside the frozen bundle, so it must never be offered by accident."""
    assert "prior_incidents" not in {s["name"] for s in BundleTools.SPECS}
    assert "prior_incidents" in {s["name"] for s in BundleTools.OPTIONAL_SPECS}
