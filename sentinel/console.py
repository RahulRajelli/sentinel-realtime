"""Live operator console (Phase R6).

A terminal view of what the aircraft is doing and what the system is advising. Every
advisory carries its evidence -- metric, measured value, threshold -- because
SENTINEL-REALTIME-MVP-SPEC.md requires each claim to cite a field and timestamp that
resolve in the retained window. An advisory without that is unverifiable and does not
belong on the screen.

No curses, no refresh tricks: append-only lines, so the session is a transcript that can be
pasted into a report or diffed between runs.
"""

from flightdx.schema import Incident

from sentinel.gate import Advisory

SEVERITY_MARK = {"info": "i", "warning": "!", "critical": "!!"}


def _evidence_line(inc: Incident, max_items: int = 2) -> str:
    parts = []
    for ev in inc.evidence[:max_items]:
        parts.append(f"{ev.metric}={ev.value:g} (thr {ev.threshold:g}{' ' + ev.unit if ev.unit else ''})")
    return "; ".join(parts) if parts else "no evidence recorded"


class Console:
    def __init__(self, show_quiet_cycles: bool = True) -> None:
        self.show_quiet_cycles = show_quiet_cycles
        self._last_quiet_line = 0.0

    def header(self, params: dict, motors: list[str]) -> None:
        print("=" * 78)
        print("SENTINEL — live advisory console")
        print("=" * 78)
        thresholds = {k: params.get(k) for k in ("MOT_PWM_MIN", "MOT_PWM_MAX",
                                                 "BATT_LOW_VOLT", "BATT_CRT_VOLT")}
        print(f"  thresholds from vehicle : {thresholds}")
        print(f"  motor outputs           : {', '.join(motors) if motors else 'undeclared'}")
        print(f"  {'time':>7}  {'state':<26} advisories")
        print("-" * 78)

    def cycle(self, t: float, telemetry: dict, raised: list[Advisory],
              active_count: int) -> None:
        state = (f"vibeZ {telemetry.get('vibe_z', 0.0):5.1f}  "
                 f"mot {telemetry.get('servo_max', 0):.0f}  "
                 f"alt {telemetry.get('alt', 0.0):5.1f}m")

        if not raised:
            if self.show_quiet_cycles and t - self._last_quiet_line >= 5.0:
                suffix = f"{active_count} active" if active_count else "clear"
                print(f"  {t:6.1f}s  {state:<26} {suffix}")
                self._last_quiet_line = t
            return

        self._last_quiet_line = t
        for adv in raised:
            inc = adv.incident
            mark = SEVERITY_MARK.get(inc.severity, "?")
            label = f"{mark} {inc.type.upper()} [{inc.severity}]"
            tag = {"new": "", "escalated": "  (WORSENED)",
                   "reminder": f"  (still active {t - adv.first_seen:.0f}s)"}.get(adv.reason, "")
            print(f"  {t:6.1f}s  {state:<26} {label}{tag}")
            print(f"  {'':>7}  {'':<26} evidence: {_evidence_line(inc)}")
            if inc.notes:
                print(f"  {'':>7}  {'':<26} {inc.notes[0]}")

    def summary(self, stats, cycles: int, duration: float) -> None:
        print("-" * 78)
        print("SESSION SUMMARY")
        print(f"  cycles                 : {cycles} over {duration:.0f}s")
        print(f"  detector incidents     : {stats.seen}")
        print(f"  advisories raised      : {stats.raised}   {dict(stats.per_reason)}")
        print(f"  suppressed by gate     : {stats.suppressed}")
        print(f"  suppression ratio      : {stats.suppression_ratio:.1%}")
        print(f"  faults cleared         : {stats.cleared}")
        if stats.seen and stats.raised:
            print(f"  reduction              : {stats.seen}:{stats.raised} "
                  f"({stats.seen / stats.raised:.1f}x fewer operator interrupts)")
        print("=" * 78)
