"""Schema migration, offline.

Why this exists. On 2026-08-14 `_identity_payload` began excluding `_TIMING_FIELDS` -- a correct
fix, because replaying one log twice was producing two different ids. What it changed was the
MEANING of `bundle_id`, and nothing recorded that. 13 untouched captures began failing their hash
and reporting themselves as edited.

A version check alone does not solve that: refusing to load an old file orphans the archive, and
one of those files was the only evidence behind a published number. Migration solves it, but only
if it cannot be used to launder a file that really was altered. That is what these tests pin.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from flightdx.schema import Evidence, Incident
from sentinel.bundle import SCHEMA_VERSION, AdvisoryRecord, CycleRecord, RunBundle
from sentinel.judges import Citation, Verdict
from sentinel.score import score_all


@pytest.fixture()
def tmpdir():
    d = Path(__file__).resolve().parent / ".tmp-migration"
    d.mkdir(exist_ok=True)
    yield d
    for f in d.glob("*.json"):
        f.unlink(missing_ok=True)


def _bundle() -> RunBundle:
    inc = Incident(t_start=9.0, t_end=10.0, type="compass_inconsistency", severity="warning",
                   evidence=[Evidence(metric="mag", value=2.6, threshold=1.0, unit="")])
    return RunBundle(
        scenario="compass_offset", expected_root_cause="compass_inconsistency",
        t_inject=8.0, inject_verified=True,
        cycles=[CycleRecord(t=10.0, incidents=[inc], detect_ms=4.2, build_ms=1.1,
                            messages_in=99, per_detector_ms={"compass": 0.4})],
        advisories=[AdvisoryRecord(t=10.0, type="compass_inconsistency",
                                   severity="warning", reason="new")],
    )


def _write_as_v1(b: RunBundle, path: Path, include_timing: bool) -> Path:
    """Write a file exactly as an older build would have: version 1, that build's id."""
    payload = b.model_dump(mode="json")
    payload["schema_version"] = 1
    payload["bundle_id"] = b._identity_hash(1, include_timing=include_timing)
    payload["params_hash"] = b.params_hash
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_a_pre_fix_v1_bundle_migrates_and_loads(tmpdir):
    """Written before the identity fix: hashes WITH timing fields included."""
    b = _bundle()
    p = _write_as_v1(b, tmpdir / f"{uuid4().hex}.json", include_timing=True)
    loaded = RunBundle.load(p)
    assert loaded.schema_version == SCHEMA_VERSION
    assert loaded.scenario == "compass_offset"


def test_a_post_fix_but_unbumped_v1_bundle_also_migrates(tmpdir):
    """The case that was forgotten first time and declared 27 authentic bundles altered.

    Written after the identity fix but before the version was bumped: version 1, timing EXCLUDED.
    """
    b = _bundle()
    p = _write_as_v1(b, tmpdir / f"{uuid4().hex}.json", include_timing=False)
    assert RunBundle.load(p).schema_version == SCHEMA_VERSION


def test_a_current_bundle_round_trips(tmpdir):
    p = _bundle().save(tmpdir / f"{uuid4().hex}.json")
    loaded = RunBundle.load(p)
    assert loaded.schema_version == SCHEMA_VERSION
    assert loaded.bundle_id == _bundle().bundle_id


def test_migration_refuses_a_tampered_v1_file(tmpdir):
    """The line that keeps migration from becoming a laundering path."""
    b = _bundle()
    p = _write_as_v1(b, tmpdir / f"{uuid4().hex}.json", include_timing=True)
    raw = json.loads(p.read_text())
    raw["advisories"].append({"t": 99.0, "type": "gps_fix_loss",
                              "severity": "critical", "reason": "forged"})
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="genuinely been altered"):
        RunBundle.load(p)


def test_an_unknown_future_version_is_refused_not_guessed(tmpdir):
    p = tmpdir / f"{uuid4().hex}.json"
    raw = _bundle().model_dump(mode="json")
    raw["schema_version"] = SCHEMA_VERSION + 5
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="newer than this build"):
        RunBundle.load(p)


def test_a_version_with_no_migrator_names_the_gap(tmpdir):
    p = tmpdir / f"{uuid4().hex}.json"
    raw = _bundle().model_dump(mode="json")
    raw["schema_version"] = 0
    p.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="no migrator"):
        RunBundle.load(p)


def test_every_version_below_current_has_a_migrator():
    """A bump without a migrator orphans an archive. That has happened once already."""
    missing = [v for v in range(1, SCHEMA_VERSION) if v not in RunBundle.MIGRATIONS]
    assert not missing, f"schema versions with no migrator: {missing}"


def test_legacy_identities_are_distinct_from_the_current_one():
    b = _bundle()
    assert b.bundle_id not in b.legacy_identities()
    assert len(b.legacy_identities()) == 2


# --- bundle_id as a FOREIGN KEY -------------------------------------------------------------
#
# The migration above fixed the bundles and broke everything that POINTED at them. On 2026-08-15
# all 60 committed verdict files began failing with "verdict cites unknown bundle": each stores
# the `bundle_id` it was scored against, and the v1 -> v2 migration recomputed every one of them.
# Nothing was wrong with the data -- the join key had moved. These pin the resolution.


def test_a_verdict_citing_a_legacy_id_still_scores(tmpdir):
    """The regression itself: a pre-migration verdict must still join to its flight."""
    b = _bundle()
    legacy = b._identity_hash(1, include_timing=False)
    assert legacy != b.bundle_id

    v = Verdict(judge="B1", bundle_id=legacy, root_cause="compass_inconsistency",
                citations=[Citation(metric="mag", t=10.0)])
    rows = score_all([b], [v])
    assert len(rows) == 1
    assert rows[0].score == 1.0


def test_an_aliased_match_is_recorded_in_the_row(tmpdir):
    """A join that repairs itself invisibly is one you stop being able to audit."""
    b = _bundle()
    legacy = b._identity_hash(1, include_timing=False)
    row = score_all([b], [Verdict(judge="B1", bundle_id=legacy,
                                  root_cause="compass_inconsistency",
                                  citations=[Citation(metric="mag", t=10.0)])])[0]
    assert any(legacy in n and b.bundle_id in n for n in row.notes), row.notes


def test_a_verdict_for_a_different_flight_is_still_refused(tmpdir):
    """The guard must not have been weakened into 'accept anything'."""
    b = _bundle()
    with pytest.raises(ValueError, match="unknown bundle"):
        score_all([b], [Verdict(judge="B1", bundle_id="deadbeefdeadbeef",
                                root_cause="compass_inconsistency")])


def test_resolvable_identities_is_current_plus_legacy():
    b = _bundle()
    assert b.resolvable_identities() == {b.bundle_id} | b.legacy_identities()


def test_an_ambiguous_legacy_id_is_refused_rather_than_guessed(monkeypatch):
    """Two flights that once hashed alike must fail loudly, not score against a coin flip."""
    a, c = _bundle(), _bundle()
    object.__setattr__(c, "scenario", "vibration")
    assert a.bundle_id != c.bundle_id

    shared = "5ha5ed1dfeedface"
    monkeypatch.setattr(type(a), "legacy_identities", lambda self: {shared})

    with pytest.raises(ValueError, match="claimed by more than one bundle"):
        score_all([a, c], [Verdict(judge="B1", bundle_id=shared,
                                   root_cause="compass_inconsistency")])
