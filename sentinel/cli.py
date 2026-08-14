"""`sentinel` -- one command for people who fly aircraft, not people who write Python.

The rest of this package is an evaluation harness with a research shape: bundles, judges,
scorers, sweeps. That is the right shape for measuring an agent and the wrong shape for a
maintenance engineer holding a log file and one question -- *what actually went wrong?*

So this file adds exactly one thing: a front door.

  sentinel doctor                     is my machine set up, and what is missing
  sentinel analyze FLIGHT.BIN         a log you already have -> plain-English findings
  sentinel watch --conn COM5,57600    live aircraft or SITL -> advisories as they happen
  sentinel capture / judge / report   the research path, unchanged

`analyze` is the one that matters for adoption: it needs no simulator, no MAVLink link, no
Python knowledge, and it runs on the `.BIN` files an operator already has sitting on an SD card.

Findings are printed with their evidence -- measured value against the threshold that was
actually loaded on the aircraft -- because an advisory a reader cannot check is one they have to
take on faith, and this project's whole argument is that they should not have to.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_FLIGHTDX_SRC = _ROOT.parent / "ardupilot-log-analyzer" / "src"
if _FLIGHTDX_SRC.exists() and str(_FLIGHTDX_SRC) not in sys.path:
    sys.path.insert(1, str(_FLIGHTDX_SRC))

MARK = {"info": "  i", "warning": "  !", "critical": " !!"}

# Plain-English gloss per detector finding: what it means, and what a human should do next.
# Deliberately phrased as "check this", never "replace that" -- the system observes, it does not
# diagnose a part, and overclaiming here would undo the honesty the rest of the project buys.
EXPLAIN: dict[str, tuple[str, str]] = {
    "vibration_excessive": (
        "Airframe vibration is above the level the autopilot's filters expect.",
        "Check motor mounts, prop balance and damping under the flight controller."),
    "accel_clipping": (
        "The accelerometer hit its measurement limit, so some motion went unrecorded.",
        "Usually downstream of vibration -- treat the vibration first, not the sensor."),
    "gps_fix_loss": (
        "The GPS lost a usable position fix during the flight.",
        "Check antenna placement and shielding; review where in the flight it happened."),
    "gps_high_hdop": (
        "GPS position quality degraded (satellite geometry was poor).",
        "Often environmental -- terrain, buildings, interference. Compare against site."),
    "ekf_inconsistency": (
        "The state estimator's confidence dropped: its sensors stopped agreeing.",
        "This is usually a SYMPTOM. Look for the sensor fault that caused it."),
    "compass_inconsistency": (
        "Magnetometer readings disagree with the rest of the estimate.",
        "Check compass calibration and separation from power wiring and payload."),
    "actuator_saturation": (
        "One or more motors were commanded to their limit and had no authority left.",
        "Check for asymmetry, overload, or wind exceeding what the airframe can hold."),
    "control_oscillation": (
        "The aircraft oscillated around a control axis.",
        "Usually control-loop tuning; compare against the rate gains in the parameters."),
    "battery_voltage_sag": (
        "Battery voltage dropped further under load than expected.",
        "Check pack health, cell balance and whether the pack is sized for this airframe."),
    "battery_threshold_misconfigured": (
        "The configured battery failsafe thresholds look inconsistent with the pack.",
        "Review BATT_LOW_VOLT / BATT_CRT_VOLT against the pack's real chemistry."),
}


def _detectors():
    """Import detectors directly, not via `sentinel.runner`.

    `runner` pulls in the MAVLink adapter, which needs pymavlink. Offline log analysis must work
    on a machine that has never installed it -- that is most of the machines this will run on.
    """
    from flightdx.detectors.actuator import detect_actuator
    from flightdx.detectors.battery import detect_battery
    from flightdx.detectors.compass import detect_compass
    from flightdx.detectors.ekf import detect_ekf
    from flightdx.detectors.gps import detect_gps
    from flightdx.detectors.oscillation import detect_oscillation
    from flightdx.detectors.vibration import detect_vibration
    return [("vibration", detect_vibration), ("ekf", detect_ekf),
            ("actuator", detect_actuator), ("battery", detect_battery),
            ("gps", detect_gps), ("compass", detect_compass),
            ("oscillation", detect_oscillation)]


# --- doctor ---------------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    """Say what is installed, what is missing, and the exact command to fix it."""
    print("sentinel doctor\n" + "=" * 60)
    ok = True

    print(f"  python              {sys.version.split()[0]}")
    if sys.version_info < (3, 11):
        print("     -> needs 3.11+"); ok = False

    for mod, why, fix in [
        ("pydantic", "required", "pip install pydantic"),
        ("flightdx", "required -- the detectors", "pip install -e ../ardupilot-log-analyzer"),
        ("pymavlink", "only for live/SITL", "pip install pymavlink"),
        ("anthropic", "only for the agent layer", "pip install anthropic"),
    ]:
        try:
            __import__(mod)
            print(f"  {mod:<20}installed        ({why})")
        except ImportError:
            required = why.startswith("required")
            print(f"  {mod:<20}MISSING          ({why})   fix: {fix}")
            if required:
                ok = False

    import os
    key = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    print(f"  API key             {'set' if key else 'not set'}          "
          f"(only for the agent layer)")

    print("\n  " + ("ready: try  sentinel analyze YOURFLIGHT.BIN" if ok
                    else "not ready -- install the items marked required above"))
    return 0 if ok else 1


# --- analyze --------------------------------------------------------------------------------

def cmd_analyze(args) -> int:
    """Analyse a .BIN log already on disk. No simulator, no link, no API key."""
    path = Path(args.log)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    try:
        from flightdx.parsers.dataflash import parse_dataflash
    except ImportError:
        print("flightdx is not installed. Run:  sentinel doctor", file=sys.stderr)
        return 1

    print(f"reading {path.name} ...")
    log = parse_dataflash(str(path))

    incidents = []
    skipped: list[str] = []
    for name, fn in _detectors():
        try:
            incidents.extend(fn(log))
        except Exception as exc:
            # One detector failing must not lose the other six. Reported, never swallowed --
            # a silent skip would look identical to "nothing was wrong".
            skipped.append(f"{name} ({type(exc).__name__})")

    print(f"\n{'=' * 72}")
    print(f"FLIGHT REPORT  |  {path.name}")
    print("=" * 72)
    print(f"  parameters loaded : {len(log.params)}")
    print(f"  message types     : {len(log.messages)}")
    if skipped:
        print(f"  detectors skipped : {', '.join(skipped)}")

    if not incidents:
        print("\n  No findings. Every check that could run came back clean.")
        print("  Note: a clean report means nothing crossed a threshold -- not that")
        print("  the flight was perfect.")
        return 0

    order = {"critical": 0, "warning": 1, "info": 2}
    incidents.sort(key=lambda i: (order.get(i.severity, 3), i.t_start))

    by_type: dict[str, list] = {}
    for inc in incidents:
        by_type.setdefault(inc.type, []).append(inc)

    print(f"\n  {len(by_type)} finding(s), most serious first:\n")
    for itype, group in sorted(by_type.items(),
                               key=lambda kv: order.get(kv[1][0].severity, 3)):
        worst = min(group, key=lambda i: order.get(i.severity, 3))
        meaning, action = EXPLAIN.get(itype, ("", ""))
        print(f"{MARK.get(worst.severity, '   ')}  {itype}   [{worst.severity}]"
              f"   {len(group)} occurrence(s), first at t={group[0].t_start:.1f}s")
        if meaning:
            print(f"       what it means : {meaning}")
            print(f"       what to check : {action}")
        for ev in worst.evidence[:2]:
            unit = f" {ev.unit}" if ev.unit else ""
            print(f"       evidence      : {ev.metric} = {ev.value:g}{unit} "
                  f"(threshold {ev.threshold:g}{unit})")
        print()

    if args.html:
        from sentinel import report_html
        out = report_html.write(args.html, source_name=path.name,
                                params_count=len(log.params), message_types=len(log.messages),
                                incidents=incidents, explain=EXPLAIN, skipped=skipped)
        print(f"  report written: {out}   (self-contained HTML -- open it or email it)\n")

    print("-" * 72)
    print("  The FIRST alarm is not always the CAUSE. One fault trips several")
    print("  detectors, and the fastest detector is not the root cause -- that")
    print("  judgement is what the agent layer measures. See the README.")
    return 0


# --- replay ---------------------------------------------------------------------------------

def cmd_replay(args) -> int:
    """Drive the realtime tier from a recorded flight, and write a bundle.

    The honest middle ground between "analyse a log" and "fly SITL": real telemetry, but through
    the streaming path with the escalation gate, so the advisory stream is what an operator would
    actually have seen in flight rather than a batch summary written afterwards.
    """
    path = Path(args.log)
    if not path.exists():
        print(f"no such file: {path}", file=sys.stderr)
        return 1

    from flightdx.parsers.dataflash import parse_dataflash

    from sentinel.replay import replay_log

    print(f"parsing {path.name} for its parameter block ...")
    parsed = parse_dataflash(str(path))
    print(f"  {len(parsed.params)} parameters -- detector thresholds come from the aircraft\n")

    print(f"{'t':>9}  advisory")
    print("-" * 72)
    seen: set[str] = set()

    def on_cycle(report, gate):
        for adv in gate.active.values():
            if adv.key in seen:
                continue
            seen.add(adv.key)
            inc = adv.incident
            ev = "; ".join(f"{e.metric}={e.value:g} (thr {e.threshold:g})"
                           for e in inc.evidence[:1]) or "no evidence"
            print(f"{report.t:9.1f}s  {MARK.get(inc.severity, '   ')} {inc.type}"
                  f"[{inc.severity}]  {ev}")

    bundle, reports = replay_log(path, cadence_s=args.cadence, window_s=args.window,
                                 params=parsed.params, on_cycle=on_cycle)

    m = bundle.metrics
    print("-" * 72)
    print(f"  cycles            : {m.cycles}")
    print(f"  raw detections    : {m.incidents}")
    print(f"  advisories raised : {m.advisories}   ({m.suppression:.1%} suppressed by the gate)")
    print(f"  detector cost     : {m.detect_ms_first:.2f} -> {m.detect_ms_last:.2f} ms/cycle")
    print(f"  worst cycle       : {m.worst_cycle_ms:.1f} ms  (processing cost, not realtime "
          f"headroom -- replay runs on log time)")

    if args.html:
        from sentinel import report_html
        incidents = [i for c in bundle.cycles for i in c.incidents]
        rep = report_html.write(
            args.html, source_name=f"{path.name} (replayed through the realtime tier)",
            params_count=len(parsed.params), message_types=len(parsed.messages),
            incidents=incidents, explain=EXPLAIN,
            gate_stats={"suppression": m.suppression, "advisories": m.advisories},
            bundle_id=bundle.bundle_id)
        print(f"  report written: {rep}")

    out = Path(args.out or f"bundles/replay_{path.stem}.json")
    bundle.save(out)
    print(f"\n  bundle: {out}   id={bundle.bundle_id}")
    print("  ground truth is NOT set -- a replay records what was seen; a human labels what")
    print("  it means. Labelling it from the detector output would make the eval circular.")
    return 0


# --- watch ----------------------------------------------------------------------------------

def cmd_watch(args) -> int:
    """Live monitoring against a real aircraft or SITL."""
    try:
        from pymavlink import mavutil
    except ImportError:
        print("live monitoring needs pymavlink:  pip install pymavlink", file=sys.stderr)
        return 1

    from sentinel.console import Console
    from sentinel.gate import EscalationGate
    from sentinel.runner import LiveRunner

    print(f"connecting {args.conn} ...")
    conn = mavutil.mavlink_connection(args.conn, dialect="ardupilotmega")
    if conn.wait_heartbeat(timeout=30) is None:
        print("no heartbeat -- check the link, the port and the baud rate", file=sys.stderr)
        return 1

    runner = LiveRunner(conn, cadence_s=args.cadence, window_s=120.0)
    if args.passive:
        # Listen at whatever rate the ground station already set, and skip the parameter
        # fetch. On a shared radio link, raising stream rates can congest the pilot's own
        # telemetry -- so this mode trades verdict quality for not touching the aircraft, and
        # says so rather than quietly degrading.
        print("passive mode: not requesting streams, not fetching parameters.")
        print("  actuator and battery verdicts will be unreliable without thresholds.")
    else:
        runner.request_streams(rate_hz=args.rate)
        runner.fetch_params()

    gate = EscalationGate()
    console = Console()
    motors = sorted(k.replace("_FUNCTION", "") for k, v in runner.params.items()
                    if k.endswith("_FUNCTION") and 33 <= int(v) <= 44)
    console.header(runner.params, motors)

    def on_cycle(r):
        raised = gate.submit(r.incidents, r.t)
        for adv in raised:
            inc = adv.incident
            ev = "; ".join(f"{e.metric}={e.value:g} (thr {e.threshold:g})"
                           for e in inc.evidence[:2]) or "no evidence recorded"
            print(f"{MARK.get(inc.severity, '   ')}  {r.t:7.1f}s  {inc.type}"
                  f"[{inc.severity}]  {ev}")

    runner.run(args.duration, on_cycle=on_cycle)
    print(f"\n  {gate.stats.raised} advisories from {gate.stats.seen} raw detections "
          f"({gate.stats.suppression_ratio:.1%} suppressed by the escalation gate)")
    return 0


# --- passthroughs ----------------------------------------------------------------------------

def _run_script(name: str, argv: list[str]) -> int:
    import runpy
    sys.argv = [name] + argv
    try:
        runpy.run_path(str(_ROOT / "scripts" / name), run_name="__main__")
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sentinel",
        description="Fault detection and root-cause analysis for ArduPilot aircraft.",
        epilog="start with:  sentinel doctor")
    sub = ap.add_subparsers(dest="command")

    sub.add_parser("doctor", help="check this machine is set up").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("analyze", help="analyse a .BIN log you already have")
    p.add_argument("log", help="path to an ArduPilot dataflash .BIN log")
    p.add_argument("--html", default=None, metavar="FILE",
                   help="also write a self-contained HTML report you can email")
    p.set_defaults(fn=cmd_analyze)

    p = sub.add_parser("replay", help="replay a real log through the realtime tier")
    p.add_argument("log", help="path to an ArduPilot dataflash .BIN log")
    p.add_argument("--cadence", type=float, default=1.0)
    p.add_argument("--window", type=float, default=120.0)
    p.add_argument("--out", default=None, help="bundle path (default bundles/replay_<name>.json)")
    p.add_argument("--html", default=None, metavar="FILE",
                   help="also write a self-contained HTML report you can email")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("watch", help="live monitoring over MAVLink")
    p.add_argument("--conn", default="tcp:127.0.0.1:5760",
                   help="COM5,57600 for a radio | udp:0.0.0.0:14550 for WiFi | "
                        "tcp:127.0.0.1:5760 for SITL (default)")
    p.add_argument("--duration", type=float, default=300.0)
    p.add_argument("--cadence", type=float, default=1.0)
    p.add_argument("--rate", type=float, default=10.0)
    p.add_argument("--passive", action="store_true",
                   help="do not change stream rates or fetch parameters (shared links)")
    p.set_defaults(fn=cmd_watch)

    for name, script, helptext in [
        ("capture", "r7_r8_scenarios.py", "fly SITL fault scenarios and record bundles"),
        ("judge", "e4_judge.py", "run every judge over recorded bundles"),
        ("report", "e4_report.py", "regenerate the measured comparison table"),
    ]:
        sp = sub.add_parser(name, help=helptext, add_help=False)
        sp.set_defaults(fn=lambda a, s=script: _run_script(s, a.rest))
        sp.add_argument("rest", nargs=argparse.REMAINDER)

    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
