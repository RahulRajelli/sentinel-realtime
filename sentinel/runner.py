"""Rolling-buffer live runner (Phase R4).

Consumes a live MAVLink link, converts it with flightdx's MavlinkAdapter, accumulates the
resulting dataflash records in a bounded window, and re-runs the batch detectors on a
fixed cadence.

Deliberately the naive design. It re-scans the whole window each cycle rather than
maintaining incremental detector state, which is affordable because the detectors were
measured at 0.00-0.01 s on an 82,670-message log -- so a 1 Hz re-scan costs well under a
tenth of a cycle. Phase E1 replaces this with true streaming detectors once R8 measures
where buffer growth actually starts to hurt; until then this is the cheapest thing that
produces real numbers.

What it is NOT: an escalation gate. Every cycle reports whatever the detectors currently
see, so a sustained fault re-reports each cycle. Deduplication with cooldowns and
severity-increase triggers is R5.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from flightdx.detectors.actuator import detect_actuator
from flightdx.detectors.battery import detect_battery
from flightdx.detectors.compass import detect_compass
from flightdx.detectors.ekf import detect_ekf
from flightdx.detectors.gps import detect_gps
from flightdx.detectors.oscillation import detect_oscillation
from flightdx.detectors.vibration import detect_vibration
from flightdx.live.adapter import MavlinkAdapter
from flightdx.schema import Incident, ParsedLog
from flightdx.signals import build_signals

from sentinel.coverage import detector_coverage
from sentinel.params import ParamCache

# timeline and errors are omitted: they read log.modes / log.events, which are built by the
# file parser from MODE and ERR records rather than from the message stream. Mode tracking
# on the live path is future work, not a silent gap.
DETECTORS: list[tuple[str, Callable[[ParsedLog], list[Incident]]]] = [
    ("vibration", detect_vibration),
    ("ekf", detect_ekf),
    ("actuator", detect_actuator),
    ("battery", detect_battery),
    ("gps", detect_gps),
    ("compass", detect_compass),
    ("oscillation", detect_oscillation),
]

# Explicit per-message requests. REQUEST_DATA_STREAM is deprecated and only partially
# honoured -- measured on this firmware it delivered HEARTBEAT/GPS_RAW_INT/EKF_STATUS_REPORT
# but silently never sent GLOBAL_POSITION_INT.
STREAM_MSG_IDS = {
    "HEARTBEAT": 0, "SYS_STATUS": 1, "ATTITUDE": 30, "ATTITUDE_TARGET": 83,
    "GLOBAL_POSITION_INT": 33, "NAV_CONTROLLER_OUTPUT": 62, "VFR_HUD": 74,
    "SERVO_OUTPUT_RAW": 36, "GPS_RAW_INT": 24, "BATTERY_STATUS": 147,
    "EKF_STATUS_REPORT": 193, "VIBRATION": 241, "SCALED_IMU2": 116,
}


@dataclass
class CycleReport:
    """One detector pass. The timing fields feed R8's crossover measurement."""

    t: float
    incidents: list[Incident]
    buffer_records: int
    detect_ms: float
    build_ms: float
    messages_in: int
    per_detector_ms: dict[str, float] = field(default_factory=dict)

    # Which detectors could evaluate this window, and why not where they could not.
    #
    # A detector that RAISES is already loud: per_detector_ms records -1.0 and the loop prints.
    # A detector that returns [] because its inputs were missing or too slow is silent, and
    # indistinguishable from one that looked and found nothing. `detect_oscillation` does exactly
    # that below 7 Hz of ATT. Absence of an advisory has meant two opposite things; this field
    # separates them.
    coverage: dict[str, str] = field(default_factory=dict)

    @property
    def total_ms(self) -> float:
        return self.build_ms + self.detect_ms


class RollingBuffer:
    """Bounded window of dataflash-shaped records, keyed by message type."""

    def __init__(self, window_s: float = 120.0) -> None:
        self.window_s = window_s
        self.messages: dict[str, list[dict]] = {}
        self._latest_t = 0.0

    def add(self, mtype: str, rec: dict) -> None:
        self.messages.setdefault(mtype, []).append(rec)
        t = rec.get("t", 0.0)
        if t > self._latest_t:
            self._latest_t = t

    def prune(self) -> None:
        """Drop records older than the window. Keeps memory bounded on long flights."""
        cutoff = self._latest_t - self.window_s
        if cutoff <= 0:
            return
        for mtype, recs in self.messages.items():
            if recs and recs[0].get("t", 0.0) < cutoff:
                # Records arrive in time order, so a linear scan from the front suffices.
                keep = 0
                for keep, rec in enumerate(recs):
                    if rec.get("t", 0.0) >= cutoff:
                        break
                self.messages[mtype] = recs[keep:]

    def record_count(self) -> int:
        return sum(len(v) for v in self.messages.values())


class LiveRunner:
    """Drives one MAVLink link through the adapter and the detectors."""

    def __init__(
        self,
        conn,
        cadence_s: float = 1.0,
        window_s: float = 120.0,
        warmup_s: float = 3.0,
    ) -> None:
        self.conn = conn
        self.cadence_s = cadence_s
        self.warmup_s = warmup_s
        self.adapter = MavlinkAdapter()
        self.buffer = RollingBuffer(window_s=window_s)
        self.params: dict[str, float] = {}
        self.param_cache: ParamCache | None = None
        self.messages_seen = 0
        self.t0 = 0.0

    def request_streams(self, rate_hz: float = 10.0) -> None:
        from pymavlink import mavutil

        interval_us = int(1_000_000 / rate_hz)
        for mid in STREAM_MSG_IDS.values():
            self.conn.mav.command_long_send(
                self.conn.target_system, self.conn.target_component,
                mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0,
                mid, interval_us, 0, 0, 0, 0, 0,
            )
            time.sleep(0.02)

    def fetch_params(self, timeout: float = 20.0) -> dict[str, float]:
        self.param_cache = ParamCache(self.conn)
        self.params = self.param_cache.fetch(timeout=timeout)
        return self.params

    def _now(self) -> float:
        return time.monotonic() - self.t0

    def build_log(self) -> ParsedLog:
        """Assemble a ParsedLog from the current window."""
        return ParsedLog(
            messages=self.buffer.messages,
            signals=build_signals(self.buffer.messages),
            params=self.params,
        )

    def run_cycle(self) -> CycleReport:
        t_build = time.perf_counter()
        log = self.build_log()
        build_ms = (time.perf_counter() - t_build) * 1000.0

        # Computed once per cycle from the same log the detectors are about to read, so it
        # reflects what they were actually given rather than what the link looked like earlier.
        coverage = detector_coverage(log)

        incidents: list[Incident] = []
        per_detector: dict[str, float] = {}
        t_detect = time.perf_counter()
        for name, fn in DETECTORS:
            t_one = time.perf_counter()
            try:
                incidents.extend(fn(log))
            except Exception as exc:  # a live tier must not die on one bad detector
                per_detector[name] = -1.0
                print(f"  ! detector {name} raised {type(exc).__name__}: {exc}")
                continue
            per_detector[name] = (time.perf_counter() - t_one) * 1000.0
        detect_ms = (time.perf_counter() - t_detect) * 1000.0

        return CycleReport(
            t=self._now(),
            incidents=incidents,
            buffer_records=self.buffer.record_count(),
            detect_ms=detect_ms,
            build_ms=build_ms,
            messages_in=self.messages_seen,
            per_detector_ms=per_detector,
            coverage={k: v["status"] for k, v in coverage.items()},
        )

    def run(
        self,
        duration_s: float,
        on_cycle: Callable[[CycleReport], None] | None = None,
    ) -> list[CycleReport]:
        """Pump the link, running detectors every cadence_s. Returns every cycle report."""
        self.t0 = time.monotonic()
        reports: list[CycleReport] = []
        next_cycle = self.warmup_s
        end = self.warmup_s + duration_s

        while self._now() < end:
            msg = self.conn.recv_match(blocking=True, timeout=0.2)
            if msg is not None:
                self.messages_seen += 1
                for mtype, rec in self.adapter.feed(msg, self._now()):
                    self.buffer.add(mtype, rec)

            if self._now() >= next_cycle:
                self.buffer.prune()
                report = self.run_cycle()
                reports.append(report)
                if on_cycle is not None:
                    on_cycle(report)
                next_cycle += self.cadence_s

        return reports
