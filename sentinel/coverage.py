"""Which detectors could actually run, and which quietly could not (Phase E5).

**The vulnerability this closes.** `runner.run_cycle` already handles a detector that *raises*:
it records `-1.0` in `per_detector_ms` and prints. But a detector that *returns an empty list*
because its preconditions were not met is indistinguishable from one that ran and found nothing.

`detect_oscillation` does exactly that in three places. It returns `[]` when ATT arrives below
`MIN_ATT_RATE_HZ = 7.0`, when no desired attitude is present, and when fewer than 20 samples
exist. Each is a correct decision -- counting zero crossings on aliased data would invent
oscillations -- and each is silent. Slow the telemetry link and that detector switches itself
off with no warning anywhere in the output.

**Absence of an advisory is therefore ambiguous today**: it means "no fault" OR "no detector".
Those are opposite conclusions for an operator, and the second one is the dangerous reading of
a quiet screen. This module makes the difference explicit, which is the same rule
`memory.py` applies to an empty history: absence of a record is not evidence of health.

Reported, never enforced. Nothing here suppresses a detector or changes an incident. It observes
what the detectors were given and states whether that was enough.
"""

from __future__ import annotations

from typing import Any

# Message types each detector reads. Taken from the detector sources and from the live adapter's
# PRODUCED_TYPES; a detector whose inputs are absent cannot speak, whatever the aircraft is doing.
REQUIRED_MESSAGES: dict[str, tuple[str, ...]] = {
    "vibration": ("VIBE",),
    "ekf": ("XKF4", "NKF4", "EKF4"),   # any one of these carries variances
    "actuator": ("RCOU",),
    "battery": ("BAT",),
    "gps": ("GPS",),
    "compass": ("MAG",),
    "oscillation": ("ATT",),
}

# `oscillation.py` declines below this rate rather than counting crossings on aliased data.
MIN_ATT_RATE_HZ = 7.0
MIN_ATT_SAMPLES = 20

OK = "ok"
NO_DATA = "no_data"
RATE_TOO_LOW = "rate_too_low"
TOO_FEW_SAMPLES = "too_few_samples"
NO_DESIRED_ATTITUDE = "no_desired_attitude"

# A detector in any of these states produced no incidents because it could not look, not because
# the aircraft was healthy. Callers use this rather than testing strings.
BLIND = (NO_DATA, RATE_TOO_LOW, TOO_FEW_SAMPLES, NO_DESIRED_ATTITUDE)


def _rate_hz(msgs: list[dict]) -> float:
    if len(msgs) < 2:
        return 0.0
    span = msgs[-1].get("t", 0.0) - msgs[0].get("t", 0.0)
    return (len(msgs) - 1) / span if span > 0 else 0.0


def detector_coverage(log: Any) -> dict[str, dict[str, Any]]:
    """Per detector: could it evaluate this window, and if not, why not.

    Takes a `ParsedLog`-shaped object (anything with `.messages`), so it works on the live
    rolling buffer and on a replayed file without knowing which it has.
    """
    messages: dict[str, list[dict]] = getattr(log, "messages", {}) or {}
    out: dict[str, dict[str, Any]] = {}

    for name, required in REQUIRED_MESSAGES.items():
        present = [m for m in required if messages.get(m)]
        if not present:
            out[name] = {"status": NO_DATA,
                         "detail": f"none of {list(required)} present in the window"}
            continue
        out[name] = {"status": OK, "detail": f"reading {present[0]}"}

    # Oscillation is the one with silent thresholds beyond mere presence, and the one that
    # actually bit: it needs a fast enough ATT stream AND a desired attitude to subtract.
    att = messages.get("ATT") or []
    if att:
        rate = _rate_hz(att)
        if len(att) < MIN_ATT_SAMPLES:
            out["oscillation"] = {
                "status": TOO_FEW_SAMPLES,
                "detail": f"{len(att)} ATT samples, needs >= {MIN_ATT_SAMPLES}"}
        elif rate < MIN_ATT_RATE_HZ:
            out["oscillation"] = {
                "status": RATE_TOO_LOW,
                "detail": f"ATT at {rate:.1f} Hz, needs >= {MIN_ATT_RATE_HZ} Hz; "
                          f"crossing counts on aliased data would be invented"}
        elif not any(("DesRoll" in m or "ErrRP" in m) for m in att[:50]):
            out["oscillation"] = {
                "status": NO_DESIRED_ATTITUDE,
                "detail": "no DesRoll/ErrRP: this detector measures tracking error, and without "
                          "a commanded attitude there is nothing to subtract. The vehicle sends "
                          "it via ATTITUDE_TARGET or NAV_CONTROLLER_OUTPUT"}
        else:
            out["oscillation"] = {"status": OK, "detail": f"ATT at {rate:.1f} Hz with a target"}

    return out


def blind_detectors(coverage: dict[str, dict[str, Any]]) -> list[str]:
    """Detectors that could not evaluate. Their silence carries no information."""
    return sorted(n for n, c in coverage.items() if c.get("status") in BLIND)


def summarise(coverage: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Flight-level view: what ran, what was blind, and why.

    The `note` is written for whoever reads a quiet report and has to decide whether quiet means
    healthy.
    """
    blind = blind_detectors(coverage)
    return {
        "detectors": len(coverage),
        "ok": sorted(n for n, c in coverage.items() if c.get("status") == OK),
        "blind": {n: coverage[n] for n in blind},
        "note": ("A blind detector produced no advisories because it could not look, not "
                 "because the aircraft was healthy. Absence of an advisory from a blind "
                 "detector is not evidence of anything."
                 if blind else
                 "Every detector had the inputs it needs, so silence from any of them is a "
                 "real observation."),
    }
