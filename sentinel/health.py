"""Whether the monitor itself is fit to be believed (Phase E5).

`coverage.py` asks whether the DETECTORS had their inputs. This asks whether the MONITOR is
keeping up. They fail independently: a perfectly fed detector set running on a process that is
30% behind real time produces advisories about a window that has already passed, and a healthy
loop reading a dead radio produces confident silence.

Three things an operator actually feels, and nothing else:

  * **The link stalled.** No telemetry for longer than a few seconds. Everything downstream is
    describing the past, and silence from the detectors means nothing at all.
  * **Packets are being lost.** MAVLink carries sequence numbers, so loss is measurable rather
    than guessable. A detector reading a decimated stream can miss a transient entirely.
  * **The loop is overrunning.** If a cycle takes longer than the cadence it was scheduled at,
    detection is no longer keeping pace with the aircraft, and the gap widens.

**Reported, never corrective.** Nothing here throttles, drops or reconnects. A monitor that
quietly repairs itself is a monitor whose degradation you find out about later, and later is
after the flight.

Thresholds are deliberately loose. This exists to catch a link that has fallen over or a loop
that is drowning, not to grade a slightly jittery radio -- a health signal that cries wolf gets
muted, and a muted health signal is worse than none.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

OK = "ok"
DEGRADED = "degraded"
STALLED = "stalled"

# No telemetry at all for this long and the monitor is describing history.
STALL_AFTER_S = 3.0
# MAVLink loss above this is enough to hide a transient between samples.
DEGRADED_LOSS_PCT = 5.0
# A cycle using more than this fraction of its cadence is close to falling behind.
BUSY_UTILISATION = 0.80


@dataclass
class HealthSnapshot:
    """One cycle's view of the monitor's own condition."""

    t: float = 0.0
    status: str = OK
    reasons: list[str] = field(default_factory=list)
    seconds_since_message: float = 0.0
    message_rate_hz: float = 0.0
    packet_loss_pct: float | None = None
    cycle_utilisation: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "seconds_since_message": round(self.seconds_since_message, 2),
            "message_rate_hz": round(self.message_rate_hz, 1),
            "packet_loss_pct": (round(self.packet_loss_pct, 2)
                                if self.packet_loss_pct is not None else None),
            "cycle_utilisation": round(self.cycle_utilisation, 3),
        }


class LinkHealth:
    """Tracks the monitor's own condition across a flight.

    Deliberately cheap: two counters and a clock. Anything that costs real time here would make
    the thing it is measuring worse.
    """

    def __init__(self, cadence_s: float = 1.0, clock=time.monotonic) -> None:
        self.cadence_s = max(cadence_s, 1e-6)
        self._clock = clock
        self._last_msg_t: float | None = None
        self._msgs_since_cycle = 0
        self._last_cycle_t: float | None = None
        self.worst: HealthSnapshot | None = None
        self.stalls = 0
        self.overruns = 0

    # ---- observers ------------------------------------------------------------------

    def on_message(self) -> None:
        self._last_msg_t = self._clock()
        self._msgs_since_cycle += 1

    def on_cycle(self, t: float, total_ms: float, conn: Any = None) -> HealthSnapshot:
        now = self._clock()
        elapsed = (now - self._last_cycle_t) if self._last_cycle_t else self.cadence_s
        self._last_cycle_t = now

        rate = self._msgs_since_cycle / elapsed if elapsed > 0 else 0.0
        self._msgs_since_cycle = 0

        since = (now - self._last_msg_t) if self._last_msg_t is not None else float("inf")
        utilisation = (total_ms / 1000.0) / self.cadence_s

        # pymavlink exposes measured loss from sequence numbers. Absent on a replayed file, and
        # None is reported rather than 0.0 -- "not measured" and "no loss" are different claims.
        loss = None
        getter = getattr(conn, "packet_loss", None)
        if callable(getter):
            try:
                loss = float(getter())
            except Exception:
                loss = None

        reasons: list[str] = []
        status = OK
        if since == float("inf"):
            status = STALLED
            reasons.append("no telemetry has arrived at all")
        elif since > STALL_AFTER_S:
            status = STALLED
            reasons.append(f"no telemetry for {since:.1f}s; advisories describe the past")
        if loss is not None and loss > DEGRADED_LOSS_PCT:
            status = STALLED if status == STALLED else DEGRADED
            reasons.append(f"{loss:.1f}% packet loss; a transient can fall between samples")
        if utilisation > 1.0:
            status = STALLED if status == STALLED else DEGRADED
            reasons.append(f"cycle took {utilisation:.0%} of its cadence; the loop is behind")
        elif utilisation > BUSY_UTILISATION:
            reasons.append(f"cycle used {utilisation:.0%} of its cadence")

        snap = HealthSnapshot(t=t, status=status, reasons=reasons,
                              seconds_since_message=(0.0 if since == float("inf") else since),
                              message_rate_hz=rate, packet_loss_pct=loss,
                              cycle_utilisation=utilisation)

        if status == STALLED:
            self.stalls += 1
        if utilisation > 1.0:
            self.overruns += 1
        # Worst cycle by status severity, then by utilisation. Kept because a flight summary that
        # averaged health would hide the ten seconds that actually mattered.
        rank = {OK: 0, DEGRADED: 1, STALLED: 2}
        if self.worst is None or (rank[status], utilisation) > (
                rank[self.worst.status], self.worst.cycle_utilisation):
            self.worst = snap
        return snap

    # ---- flight-level ---------------------------------------------------------------

    def summary(self, cycles: int) -> dict[str, Any]:
        worst = self.worst.as_dict() if self.worst else {"status": OK, "reasons": []}
        healthy = self.stalls == 0 and self.overruns == 0
        return {
            "cycles": cycles,
            "stalled_cycles": self.stalls,
            "overrun_cycles": self.overruns,
            "worst": worst,
            "note": ("The monitor kept pace with the link for the whole flight, so silence from "
                     "a detector is a real observation."
                     if healthy else
                     "The monitor was degraded for part of this flight. Advisory timing and any "
                     "absence of advisories over those cycles are unreliable."),
        }
