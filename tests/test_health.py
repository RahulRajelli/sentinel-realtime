"""Monitor self-health, offline.

`coverage.py` asks whether the detectors had their inputs. This asks whether the monitor kept
pace with the link. They fail independently, and both failures are silent by default: a well-fed
detector set running 30% behind real time produces advisories about a window that has already
passed, and a healthy loop reading a dead radio produces confident silence.

The clock is injected so these run in microseconds and test thresholds rather than wall time.
"""

from __future__ import annotations

from sentinel.health import (
    BUSY_UTILISATION, DEGRADED, DEGRADED_LOSS_PCT, OK, STALL_AFTER_S, STALLED, LinkHealth,
)


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _Conn:
    def __init__(self, loss=None):
        self._loss = loss

    def packet_loss(self):
        if self._loss is None:
            raise RuntimeError("not measurable on a replayed file")
        return self._loss


def _healthy(cadence=1.0):
    c = _Clock()
    h = LinkHealth(cadence_s=cadence, clock=c)
    h.on_message()
    return h, c


def test_a_flowing_link_within_budget_reads_ok():
    h, c = _healthy()
    c.advance(0.1)
    snap = h.on_cycle(t=1.0, total_ms=50.0, conn=_Conn(0.0))
    assert snap.status == OK and not snap.reasons


def test_a_stalled_link_is_reported_not_silent():
    """The dangerous case: detectors stay quiet and the quiet means nothing."""
    h, c = _healthy()
    c.advance(STALL_AFTER_S + 1.0)
    snap = h.on_cycle(t=5.0, total_ms=10.0, conn=_Conn(0.0))
    assert snap.status == STALLED
    assert "describe the past" in snap.reasons[0]


def test_a_link_that_never_delivered_anything_is_stalled():
    c = _Clock()
    h = LinkHealth(cadence_s=1.0, clock=c)
    snap = h.on_cycle(t=1.0, total_ms=5.0)
    assert snap.status == STALLED and "at all" in snap.reasons[0]


def test_packet_loss_above_threshold_degrades():
    h, c = _healthy()
    c.advance(0.1)
    snap = h.on_cycle(t=1.0, total_ms=10.0, conn=_Conn(DEGRADED_LOSS_PCT + 5))
    assert snap.status == DEGRADED
    assert "packet loss" in snap.reasons[0]


def test_unmeasurable_loss_is_none_not_zero():
    """'Not measured' and 'no loss' are different claims and must not be conflated."""
    h, c = _healthy()
    c.advance(0.1)
    assert h.on_cycle(t=1.0, total_ms=10.0, conn=_Conn(None)).packet_loss_pct is None
    assert h.on_cycle(t=2.0, total_ms=10.0, conn=None).packet_loss_pct is None


def test_a_cycle_over_its_cadence_is_degraded():
    h, c = _healthy(cadence=0.25)
    c.advance(0.25)
    snap = h.on_cycle(t=1.0, total_ms=300.0, conn=_Conn(0.0))   # 300ms into a 250ms cadence
    assert snap.status == DEGRADED
    assert "loop is behind" in snap.reasons[-1]
    assert snap.cycle_utilisation > 1.0


def test_a_busy_but_keeping_up_cycle_warns_without_degrading():
    h, c = _healthy(cadence=1.0)
    c.advance(1.0)
    snap = h.on_cycle(t=1.0, total_ms=1000.0 * (BUSY_UTILISATION + 0.05), conn=_Conn(0.0))
    assert snap.status == OK and snap.reasons, "should warn, not degrade"


def test_the_summary_keeps_the_worst_cycle_not_the_average():
    """Averaging health would hide the ten seconds that actually mattered."""
    h, c = _healthy()
    for _ in range(20):
        c.advance(0.1)
        h.on_message()
        h.on_cycle(t=c.t, total_ms=10.0, conn=_Conn(0.0))
    c.advance(STALL_AFTER_S + 1)
    h.on_cycle(t=c.t, total_ms=10.0, conn=_Conn(0.0))
    s = h.summary(cycles=21)
    assert s["stalled_cycles"] == 1
    assert s["worst"]["status"] == STALLED
    assert "unreliable" in s["note"]


def test_a_clean_flight_says_silence_can_be_trusted():
    h, c = _healthy()
    for _ in range(5):
        c.advance(0.1)
        h.on_message()
        h.on_cycle(t=c.t, total_ms=10.0, conn=_Conn(0.0))
    s = h.summary(cycles=5)
    assert s["stalled_cycles"] == 0 and s["overrun_cycles"] == 0
    assert "real observation" in s["note"]


def test_health_never_repairs_anything():
    """Reported, never corrective. Self-repair hides degradation until after the flight."""
    h, _ = _healthy()
    public = [n for n in dir(h) if not n.startswith("_")]
    for banned in ("reconnect", "reset", "throttle", "drop"):
        assert not any(banned in n for n in public), f"{banned} suggests corrective behaviour"
