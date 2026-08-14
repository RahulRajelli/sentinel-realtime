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

from pydantic import BaseModel, Field

from flightdx.schema import Incident

# Bumped whenever a field changes meaning. A judge that reads a bundle it does not understand
# must refuse rather than guess -- see `load()`. L2 widens the task set, so this will move.
SCHEMA_VERSION = 1


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

    def _identity_payload(self) -> dict:
        """The fields that make this bundle *this* flight.

        `created_utc` is excluded on purpose: it is provenance, not content. Including it would
        mean re-serialising an unchanged bundle produced a different id, which defeats the point
        of having one.
        """
        return {
            "schema_version": self.schema_version,
            "scenario": self.scenario,
            "expected_root_cause": self.expected_root_cause,
            "injection": [p.model_dump(mode="json") for p in self.injection],
            "t_inject": self.t_inject,
            "params_hash": self.params_hash,
            "cycles": [c.model_dump(mode="json") for c in self.cycles],
            "advisories": [a.model_dump(mode="json") for a in self.advisories],
        }

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
        canon = json.dumps(self._identity_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canon.encode()).hexdigest()[:16]

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

    @classmethod
    def load(cls, path: str | Path) -> RunBundle:
        raw = json.loads(Path(path).read_text())
        stored_id = raw.pop("bundle_id", None)
        raw.pop("params_hash", None)

        version = raw.get("schema_version", 0)
        if version != SCHEMA_VERSION:
            # Refuse rather than coerce. A judge silently reading a bundle whose fields changed
            # meaning would produce numbers that look fine and are wrong.
            raise ValueError(
                f"{path}: schema_version {version}, this build expects {SCHEMA_VERSION}")

        bundle = cls.model_validate(raw)
        if stored_id is not None and stored_id != bundle.bundle_id:
            raise ValueError(
                f"{path}: bundle_id mismatch (file says {stored_id}, "
                f"content hashes to {bundle.bundle_id}) -- the file has been edited")
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


def load_all(directory: str | Path) -> list[RunBundle]:
    """Load every bundle in a directory, in a stable order.

    Sorted by filename so a sweep's scenario order is reproducible; an unordered glob would make
    two otherwise identical runs disagree on row order in the published table.
    """
    return [RunBundle.load(p) for p in sorted(Path(directory).glob("*.json"))
            if _looks_like_bundle(p)]
