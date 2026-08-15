"""Serialised record of one captured scenario flight (Phase E4).

Why this exists at all: `scripts/r7_r8_scenarios.py` flies SITL *and* decides, in one process.
That is affordable at 4 scenarios x 1 configuration (~16 min). E4 compares four judges across
three prompt paraphrases, one of which is N-sample -- ~96 judgments over the same 4 flights.
Re-flying for each is ~6 hours per sweep and is re-paid on every code change.

So the flight is captured once into a `RunBundle` and every judge runs offline against the file.
Four consequences, all of them wanted independently:

  * deterministic replay -- same bundle + same judge + same seed reproduces the verdict, which is
    what lets someone else check the published table;
  * matched-budget baselines become affordable, and without them a B3 win is unfalsifiable;
  * judgment tests run with no WSL and no SITL;
  * the golden labels stop moving underneath the statistics.

Three things are carried deliberately rather than incidentally:

  * **`params` and `params_hash`.** Per spec section 7, a threshold verdict is meaningless without
    the thresholds. A bundle that lost them could not be re-scored honestly later.
  * **The injection readback.** `r4_fly_inject.py:73` already treats a silently-unapplied
    injection as worse than a failed one, because it turns a false negative into a fake pass.
    Recording the readback makes the bundle self-certifying about its own test condition.
  * **`flightdx.schema.Incident`, unchanged.** Also spec section 7 -- reusing it is what stops the
    captured tier from drifting away from the scorer.

Deliberately NOT here: any judgement about which fault is the root cause. A bundle records what
happened. `gate.py:21` defers that to E4 and so does this file; the judges in `sentinel/judges/`
are the only place a root cause is ever named.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, ClassVar

from pydantic import BaseModel, Field

from flightdx.schema import Incident

# Bumped whenever a field changes meaning OR the identity function changes. A judge that reads a
# bundle it does not understand must refuse rather than guess -- but refusing is not enough on its
# own, because a refusal orphans an archive. `load()` migrates forward; see MIGRATIONS.
#
# v1 -> v2 (2026-08-15): `_identity_payload` began excluding `_TIMING_FIELDS`, which changed what
# a bundle_id MEANS without anything recording that it had. 13 untouched bundles started failing
# their hash and reported themselves as edited. The version was owed on 2026-08-14 and is being
# paid now, with a migrator so no capture is lost to it.
SCHEMA_VERSION = 2


class InjectedParam(BaseModel):
    """One parameter set during injection, with the readback that proves it took.

    `applied` is not `readback == value`: SITL returns floats and several sim parameters are
    scaled on the way in, so the comparison carries the same 1% tolerance the live harness uses
    (`r7_r8_scenarios.py:242`).
    """

    name: str
    value: float
    readback: float | None = None
    applied: bool = False


class CycleRecord(BaseModel):
    """One detector pass, flattened from `runner.CycleReport`.

    `per_detector_ms` keeps the -1.0 sentinel the runner writes when a detector raised, because
    a crashed detector and a fast one must never look alike to the scorer.
    """

    t: float
    incidents: list[Incident] = Field(default_factory=list)
    buffer_records: int = 0
    detect_ms: float = 0.0
    build_ms: float = 0.0
    messages_in: int = 0
    per_detector_ms: dict[str, float] = Field(default_factory=dict)

    @property
    def total_ms(self) -> float:
        return self.build_ms + self.detect_ms


class AdvisoryRecord(BaseModel):
    """One advisory the escalation gate actually raised.

    `pre_inject` is kept per-advisory rather than recomputed from `t < t_inject`, because the
    null scenario never injects at all and would otherwise have no way to mark a false positive.
    """

    t: float
    type: str
    severity: str
    reason: str
    pre_inject: bool = False


class RunMetrics(BaseModel):
    """The R8 table's fields, carried so a bundle can be checked against `r8_results.json`.

    These are recorded, not recomputed on load: the refactor gate for this phase is that every
    one of them matches the pre-refactor run exactly. A field that gets derived instead of stored
    cannot serve as that check.
    """

    latency_s: float | None = None
    false_positives: int = 0
    incidents: int = 0
    advisories: int = 0
    suppression: float = 0.0
    cycles: int = 0
    buffer_first: int = 0
    buffer_last: int = 0
    detect_ms_first: float = 0.0
    detect_ms_last: float = 0.0
    worst_cycle_ms: float = 0.0


class RunBundle(BaseModel):
    """Everything one captured flight knows about itself.

    The ground truth (`expected_root_cause`) lives here rather than in the scorer so that a
    bundle handed to someone else is self-contained and scoreable. `expected_symptoms` is the
    cascade a correct system is *allowed* to also raise -- naming one of those as the root cause
    is the specific failure E4 exists to measure, so it has to be written down, not inferred.
    """

    schema_version: int = SCHEMA_VERSION
    scenario: str
    note: str = ""
    created_utc: str = ""

    # Which aircraft this flight was flown on. The key `memory.FlightHistory` groups by, so a
    # judge can ask whether a fault is recurring on this airframe or new to this flight.
    #
    # EXCLUDED from `_identity_payload` on purpose, and this is the one decision here that could
    # break the archive if got wrong. It is provenance, not content -- the same class as
    # `created_utc`. Two identical flights labelled with different airframes are still the same
    # flight, and including it would change every existing bundle_id and orphan the archive.
    # Because it is excluded and defaults to "", bundles written before this field still load
    # and still hash to their stored ids, so no SCHEMA_VERSION bump is required.
    airframe_id: str = ""

    # Flight-level detector coverage: which detectors could evaluate, and why not where they
    # could not. Stored as a summary rather than per cycle, because 181 cycles x 7 detectors of
    # near-identical status strings is payload with no extra information in it -- the same
    # reasoning that capped `detector_evidence`.
    #
    # EXCLUDED from `_identity_payload`, like `airframe_id`: it describes the observing
    # conditions, not what the aircraft did, and including it would change every existing hash.
    detector_coverage: dict[str, Any] = Field(default_factory=dict)

    # None means "a clean system stays quiet" -- the null and wind scenarios. Any non-null
    # verdict on those is a hallucination, which is the same gate `r7_r8_scenarios.py:272` runs.
    expected_root_cause: str | None = None
    expected_symptoms: list[str] = Field(default_factory=list)

    injection: list[InjectedParam] = Field(default_factory=list)
    t_inject: float | None = None
    inject_verified: bool = False

    params: dict[str, float] = Field(default_factory=dict)
    cycles: list[CycleRecord] = Field(default_factory=list)
    advisories: list[AdvisoryRecord] = Field(default_factory=list)
    metrics: RunMetrics = Field(default_factory=RunMetrics)

    # ---- identity -------------------------------------------------------------------

    # Wall-clock measurements. Present in the bundle because they are the E1 crossover input,
    # but EXCLUDED from identity: they record how fast this machine happened to run, not what
    # the aircraft did.
    #
    # Found 2026-08-14 by replaying one log twice and getting two different ids. Left in, the
    # hash would fingerprint the host rather than the flight -- and the claim printed next to it
    # ("anyone with the same log reproduces the same identifier") would simply be false. An
    # identifier that changes when nothing about the flight changed is worse than no identifier.
    _TIMING_FIELDS = ("detect_ms", "build_ms", "per_detector_ms", "messages_in")

    def _identity_payload(self) -> dict:
        """The fields that make this bundle *this* flight.

        `created_utc` is excluded for the same reason as the timing fields: it is provenance,
        not content. Re-serialising an unchanged bundle must produce an unchanged id.
        """
        cycles = []
        for c in self.cycles:
            d = c.model_dump(mode="json")
            for field in self._TIMING_FIELDS:
                d.pop(field, None)
            cycles.append(d)

        return {
            "schema_version": self.schema_version,
            "scenario": self.scenario,
            "expected_root_cause": self.expected_root_cause,
            "injection": [p.model_dump(mode="json") for p in self.injection],
            "t_inject": self.t_inject,
            "params_hash": self.params_hash,
            "cycles": cycles,
            "advisories": [a.model_dump(mode="json") for a in self.advisories],
        }

    def _identity_hash(self, schema_version: int, include_timing: bool) -> str:
        """One identity function, parameterised by the two things that have ever varied.

        Three combinations are legitimate in this archive, which is exactly the mess that made
        migration necessary:

          (1, timing INCLUDED)   written before 2026-08-14
          (1, timing EXCLUDED)   written after the identity fix but before the version was bumped
          (2, timing EXCLUDED)   current

        Migration must accept all three and nothing else. Hand-rolling each one invited the bug
        where the middle case was forgotten and 27 authentic bundles were declared altered.
        """
        payload = self._identity_payload()
        if include_timing:
            payload["cycles"] = [c.model_dump(mode="json") for c in self.cycles]
        payload["schema_version"] = schema_version
        canon = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()[:16]

    def legacy_identities(self) -> set[str]:
        """Every id a previous build of this code would legitimately have written."""
        return {self._identity_hash(1, include_timing=True),
                self._identity_hash(1, include_timing=False)}

    @property
    def params_hash(self) -> str:
        """Hash of the parameter set the detectors ran against.

        Spec section 7: thresholds travel with the frame, because a verdict about a threshold
        breach is unreadable without knowing the threshold. Truncated to 16 hex chars -- this
        identifies a config, it does not defend against an adversary.
        """
        canon = json.dumps(self.params, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()[:16]

    @property
    def bundle_id(self) -> str:
        """Content hash. A verdict cites this, so a result can never be silently re-attributed
        to a different flight than the one it was made against."""
        return self._identity_hash(self.schema_version, include_timing=False)

    # ---- window ---------------------------------------------------------------------

    @property
    def t_start(self) -> float:
        return self.cycles[0].t if self.cycles else 0.0

    @property
    def t_end(self) -> float:
        return self.cycles[-1].t if self.cycles else 0.0

    def contains_time(self, t: float) -> bool:
        """Whether a cited timestamp falls inside the captured window.

        The citation validator needs this: an advisory that cites a moment the bundle never
        observed is a fabrication regardless of how plausible the value looks.
        """
        if not self.cycles:
            return False
        return self.t_start <= t <= self.t_end

    # ---- persistence ----------------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not self.created_utc:
            self.created_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
        payload = self.model_dump(mode="json")
        # Derived, so they are written for the reader's benefit and ignored on load.
        payload["bundle_id"] = self.bundle_id
        payload["params_hash"] = self.params_hash
        p.write_text(json.dumps(payload, indent=1))
        return p

    # ---- migration ------------------------------------------------------------------

    @staticmethod
    def _migrate_1_to_2(raw: dict) -> dict:
        """v1 -> v2. No field changed; only what `bundle_id` means changed.

        So the migrator rewrites nothing except the version. Authenticity is checked separately
        in `load()`, against BOTH identity functions, because two kinds of v1 file exist in the
        wild: those written before the identity change (hash under v1) and those written after it
        but before the version was bumped (hash under v2). Both are genuine captures. Only a file
        matching neither has actually been altered.
        """
        raw["schema_version"] = 2
        return raw

    # ClassVar, or pydantic reads it as a model field.
    MIGRATIONS: ClassVar[dict[int, Callable[[dict], dict]]] = {1: _migrate_1_to_2}

    @classmethod
    def load(cls, path: str | Path) -> RunBundle:
        raw = json.loads(Path(path).read_text())
        stored_id = raw.pop("bundle_id", None)
        raw.pop("params_hash", None)

        version = raw.get("schema_version", 0)
        migrated_from = None
        while version < SCHEMA_VERSION:
            migrator = cls.MIGRATIONS.get(version)
            if migrator is None:
                raise ValueError(
                    f"{path}: schema_version {version} has no migrator to "
                    f"{SCHEMA_VERSION}. Re-capture the flight, or add one to MIGRATIONS")
            if migrated_from is None:
                migrated_from = version
            raw = migrator.__func__(raw) if hasattr(migrator, "__func__") else migrator(raw)
            version = raw.get("schema_version", 0)

        if version > SCHEMA_VERSION:
            # Forward-incompatible. Guessing at a field this build has never seen is how a judge
            # produces numbers that look fine and are wrong.
            raise ValueError(
                f"{path}: schema_version {version} is newer than this build ({SCHEMA_VERSION}). "
                f"Update the code rather than downgrading the file")

        bundle = cls.model_validate(raw)
        # Authenticity across a migration. A migrated file legitimately hashes under the OLDER
        # identity function, so both are accepted -- and nothing else is. This is the line that
        # keeps migration from becoming a laundering path.
        legitimate = {bundle.bundle_id}
        if migrated_from is not None:
            legitimate |= bundle.legacy_identities()

        if stored_id is not None and stored_id not in legitimate:
            # Two very different causes, and the message must not assert the wrong one.
            #
            # This said "the file has been edited" until 2026-08-14, when 13 untouched bundles
            # failed it at once. Nothing had been edited: `_identity_payload` began excluding
            # _TIMING_FIELDS that day, so bundles written before the change carry an id computed
            # by the older identity function. They passed the schema_version check because the
            # version was not bumped alongside it.
            #
            # RULE: changing `_identity_payload` or `_TIMING_FIELDS` changes what a bundle_id
            # MEANS and must bump SCHEMA_VERSION in the same commit. Then a stale archive fails
            # with "schema_version 1, expects 2" -- which names the cause -- instead of an
            # accusation that sends the reader hunting for a tamper that never happened.
            raise ValueError(
                f"{path}: bundle_id mismatch. The file says {stored_id}; it hashes to "
                f"{bundle.bundle_id} under the current identity function"
                + (f" and {sorted(bundle.legacy_identities())} under earlier ones"
                   if migrated_from is not None else "")
                + ". Every identity this build knows about has been tried, so this file has "
                  "genuinely been altered since it was written. Re-capture the flight.")
        return bundle


def _looks_like_bundle(path: Path) -> bool:
    """Cheap structural check before strict validation.

    `load_all` globs *.json, and a `verdicts.json` or a stray config sitting in the same
    directory is not a corrupt bundle -- it is not a bundle at all. Failing on it would make an
    unrelated file break the report, while skipping everything that fails to parse would hide a
    genuinely broken capture. So: skip files that lack a bundle's shape, and let everything with
    that shape go through `load()` and fail loudly if it is wrong.
    """
    try:
        raw = json.loads(path.read_text())
    except (ValueError, OSError):
        return False
    return isinstance(raw, dict) and "scenario" in raw and "cycles" in raw


def load_all(directory: str | Path, only: list[str] | None = None) -> list[RunBundle]:
    """Load every bundle in a directory, in a stable order.

    Sorted by filename so a sweep's scenario order is reproducible; an unordered glob would make
    two otherwise identical runs disagree on row order in the published table.

    `only` filters by FILENAME, before loading. That ordering is the point: bundles are named
    `{scenario}[_{tag}]_r{rep}.json`, so a name filter is a scenario filter, and applying it
    first means a scoped sweep cannot be killed by an unrelated bundle it was never going to
    judge. Measured 2026-08-14: judging the two ambiguous pairs aborted on a `gps_loss` bundle
    from an older schema -- a file the run had already excluded.

    Integrity is NOT relaxed for the bundles actually selected. A bundle that is in scope and
    fails its hash still raises, because that is the case the check exists for.
    """
    paths = [p for p in sorted(Path(directory).glob("*.json")) if _looks_like_bundle(p)]
    if only:
        paths = [p for p in paths if any(w in p.name for w in only)]
    return [RunBundle.load(p) for p in paths]
