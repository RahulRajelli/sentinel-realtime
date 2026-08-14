"""Escalation gate (Phase R5).

The R4 runner re-runs batch detectors over a rolling window every cycle, so one continuous
fault re-reports every cycle *and* accumulates: a measured 30 s vibration injection ended
with a single cycle emitting 11 separate `vibration_excessive` incidents. Raw, that is
unusable for an operator and ruinous for cost, because the LLM tier in
SENTINEL-REALTIME-MVP-SPEC.md is priced per escalation.

This collapses that stream to advisories. One fault produces one advisory, which is
re-raised only when it gets worse.

Rules, in order:
  1. Identity is (incident.type) per device -- NOT the time interval, because the interval
     of a continuous fault shifts every cycle as the window slides.
  2. A known fault at the same or lower severity is suppressed.
  3. A severity increase always escalates, ignoring cooldown: getting worse is news.
  4. Otherwise a repeat escalates only after `cooldown_s`, so a genuinely persistent fault
     still reminds the operator without spamming.
  5. A fault absent for `clear_after_s` is cleared, so its recurrence is new again.

Deliberately NOT here: any judgement about which fault is the root cause. That is the
agent's job (E4), and the spec's rule is that credit goes only to the injected root cause.
This layer must not quietly pick a winner.
"""

from dataclasses import dataclass, field

from flightdx.schema import Incident

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}


@dataclass
class Advisory:
    """One operator-facing advisory, possibly re-raised."""

    key: str
    incident: Incident
    first_seen: float
    last_raised: float
    raise_count: int = 1
    peak_severity: str = "info"
    reason: str = "new"

    @property
    def severity(self) -> str:
        return self.incident.severity


@dataclass
class GateStats:
    seen: int = 0
    raised: int = 0
    suppressed: int = 0
    cleared: int = 0
    per_reason: dict[str, int] = field(default_factory=dict)

    @property
    def suppression_ratio(self) -> float:
        """Fraction of detector output the gate absorbed. The R8 headline number."""
        return (self.suppressed / self.seen) if self.seen else 0.0


class EscalationGate:
    def __init__(self, cooldown_s: float = 30.0, clear_after_s: float = 10.0) -> None:
        self.cooldown_s = cooldown_s
        self.clear_after_s = clear_after_s
        self.active: dict[str, Advisory] = {}
        self.stats = GateStats()
        self._last_seen: dict[str, float] = {}

    @staticmethod
    def key_for(incident: Incident, device_id: str = "sitl") -> str:
        return f"{device_id}:{incident.type}"

    def submit(self, incidents: list[Incident], now: float,
               device_id: str = "sitl") -> list[Advisory]:
        """Feed one cycle's detector output. Returns advisories to raise *this* cycle."""
        raised: list[Advisory] = []

        # Collapse this cycle's duplicates first, keeping the most severe per type. Without
        # this the loop below would compare a fault against itself.
        worst: dict[str, Incident] = {}
        for inc in incidents:
            self.stats.seen += 1
            k = self.key_for(inc, device_id)
            cur = worst.get(k)
            if cur is None or SEVERITY_RANK.get(inc.severity, 0) > SEVERITY_RANK.get(cur.severity, 0):
                worst[k] = inc

        for k, inc in worst.items():
            self._last_seen[k] = now
            existing = self.active.get(k)

            if existing is None:
                adv = Advisory(key=k, incident=inc, first_seen=now, last_raised=now,
                               peak_severity=inc.severity, reason="new")
                self.active[k] = adv
                raised.append(adv)
                self.stats.raised += 1
                self.stats.per_reason["new"] = self.stats.per_reason.get("new", 0) + 1
                continue

            got_worse = (SEVERITY_RANK.get(inc.severity, 0)
                         > SEVERITY_RANK.get(existing.peak_severity, 0))
            due = (now - existing.last_raised) >= self.cooldown_s

            if got_worse or due:
                existing.incident = inc
                existing.last_raised = now
                existing.raise_count += 1
                existing.reason = "escalated" if got_worse else "reminder"
                if got_worse:
                    existing.peak_severity = inc.severity
                raised.append(existing)
                self.stats.raised += 1
                self.stats.per_reason[existing.reason] = (
                    self.stats.per_reason.get(existing.reason, 0) + 1)
            else:
                self.stats.suppressed += 1

        # Count duplicates collapsed within this cycle as suppressed too.
        self.stats.suppressed += max(0, len(incidents) - len(worst))

        self._expire(now)
        return raised

    def _expire(self, now: float) -> None:
        gone = [k for k, adv in self.active.items()
                if now - self._last_seen.get(k, adv.last_raised) > self.clear_after_s]
        for k in gone:
            del self.active[k]
            self.stats.cleared += 1
