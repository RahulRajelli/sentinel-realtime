"""Replay a recorded flight through the realtime tier (Phase E4).

**This is the answer to "is SITL realistic enough".** It is not.

SITL faults are injected by setting `SIM_*` parameters -- a clean, synthetic step change at a
known instant. Real faults do not arrive that way: they ramp, they intermit, they arrive tangled
with wind and throttle changes and a pilot reacting. A harness measured only on injected faults
measures how well it detects injections.

So this module drives the *same* rolling buffer, the *same* seven detectors and the *same*
escalation gate from a **real `.BIN` log** instead of a socket, using
`flightdx.live.replay.stream_from_log`. Every timing and suppression number it produces comes
from telemetry an aircraft actually recorded.

Three properties this buys that SITL cannot:

  * **Realism.** Real vibration, real GPS geometry, real battery sag under a real load.
  * **Verifiability by a stranger.** Anyone with the same log gets the same bundle -- no
    simulator build, no WSL, no ArduPilot toolchain. `bundle_id` is a content hash, so two
    people can check they analysed the same flight.
  * **A path off the injected-fault ceiling.** L2 needs tasks whose ground truth came from a
    real aircraft rather than a parameter poke; this is the mechanism that produces them.

What it deliberately does NOT do: invent a ground-truth label. A replayed bundle records what the
detectors saw and leaves `expected_root_cause` empty unless a human sets it. Guessing the label
from the detector output would make the eval circular -- the system would be graded against its
own opinion.

Timing note: the replayed clock is the log's own timestamps, not wall clock. Detection *latency*
is therefore honest (it is measured in flight-seconds), while per-cycle *cost* is not comparable
to the live path, which is why `RunMetrics.worst_cycle_ms` from a replay should be read as
"processing cost on this machine", never as a realtime headroom claim.
"""

from __future__ import annotations

import time
from pathlib import Path

from flightdx.live.adapter import MavlinkAdapter
from flightdx.live.replay import stream_from_log
from flightdx.schema import ParsedLog
from flightdx.signals import build_signals

from sentinel.bundle import RunBundle, RunMetrics
from sentinel.capture import BundleRecorder
from sentinel.gate import EscalationGate
from sentinel.runner import DETECTORS, CycleReport, RollingBuffer


def replay_log(
    path: str | Path,
    cadence_s: float = 1.0,
    window_s: float = 120.0,
    params: dict[str, float] | None = None,
    on_cycle=None,
) -> tuple[RunBundle, list[CycleReport]]:
    """Feed a `.BIN` through the realtime tier and return the bundle it produces.

    `params` should be the log's own parameter block when available -- the detectors read
    thresholds from it, and running with an empty dict silently falls back to defaults that may
    not match the aircraft. `sentinel replay` passes the parsed log's params for this reason.
    """
    path = Path(path)
    adapter = MavlinkAdapter()
    buffer = RollingBuffer(window_s=window_s)
    gate = EscalationGate()
    params = dict(params or {})

    recorder = BundleRecorder(
        scenario=f"replay:{path.stem}",
        note="replayed from a recorded flight; ground truth not set by the system",
    )

    reports: list[CycleReport] = []
    next_cycle = cadence_s
    latest_t = 0.0

    for msg, t in stream_from_log(str(path)):
        latest_t = t
        for mtype, rec in adapter.feed(msg, t):
            buffer.add(mtype, rec)

        if t < next_cycle:
            continue
        next_cycle = t + cadence_s

        buffer.prune()
        t_build = time.perf_counter()
        log = ParsedLog(messages=buffer.messages,
                        signals=build_signals(buffer.messages), params=params)
        build_ms = (time.perf_counter() - t_build) * 1000.0

        incidents = []
        per_detector: dict[str, float] = {}
        t_detect = time.perf_counter()
        for name, fn in DETECTORS:
            t_one = time.perf_counter()
            try:
                incidents.extend(fn(log))
            except Exception:
                # Same contract as the live runner: a crashed detector is recorded as -1.0 and
                # the other six keep running. `score.attribute` reads this as an ENVIRONMENT
                # failure, so a detector fault is never charged to a judge.
                per_detector[name] = -1.0
                continue
            per_detector[name] = (time.perf_counter() - t_one) * 1000.0
        detect_ms = (time.perf_counter() - t_detect) * 1000.0

        report = CycleReport(t=t, incidents=incidents, buffer_records=buffer.record_count(),
                             detect_ms=detect_ms, build_ms=build_ms,
                             messages_in=0, per_detector_ms=per_detector)
        reports.append(report)

        for adv in gate.submit(incidents, t):
            # Nothing is "pre-injection" in a replay -- there was no injection. Recording these
            # as pre_inject would mark every real finding a false positive.
            recorder.on_advisory(t, adv.incident, adv.reason, pre_inject=False)
        recorder.on_cycle(report)

        if on_cycle is not None:
            on_cycle(report, gate)

    first = next((a for a in recorder._advisories), None)  # noqa: SLF001 - same package
    metrics = RunMetrics(
        latency_s=None,                      # undefined without an injection instant
        false_positives=0,                   # undefined without ground truth
        incidents=gate.stats.seen,
        advisories=gate.stats.raised,
        suppression=round(gate.stats.suppression_ratio, 3),
        cycles=len(reports),
        buffer_first=reports[0].buffer_records if reports else 0,
        buffer_last=reports[-1].buffer_records if reports else 0,
        detect_ms_first=round(reports[0].detect_ms, 2) if reports else 0.0,
        detect_ms_last=round(reports[-1].detect_ms, 2) if reports else 0.0,
        worst_cycle_ms=round(max((r.total_ms for r in reports), default=0.0), 1),
    )
    bundle = recorder.finish(params=params, metrics=metrics)
    _ = (first, latest_t)
    return bundle, reports
