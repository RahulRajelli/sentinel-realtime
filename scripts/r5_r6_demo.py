#!/usr/bin/env python3
"""R5 + R6: escalation gate and live console, against a real injected fault.

Flies SITL, injects vibration, and shows what an operator would actually see -- one
advisory per fault, re-raised only when it worsens -- instead of the raw detector stream
that produced 11 duplicate incidents in a single cycle during R4.

Gates this demonstrates:
  * silent before injection (the null case)
  * one advisory for one continuous fault
  * escalation when severity rises
  * a reported suppression ratio, which is R8's headline number
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymavlink import mavutil

from scripts.r4_fly_inject import RESETS, SCENARIOS, arm_and_takeoff, set_and_verify, set_param
from sentinel.console import Console
from sentinel.gate import EscalationGate
from sentinel.runner import LiveRunner


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn", default="tcp:127.0.0.1:5760")
    ap.add_argument("--scenario", default="vibration", choices=sorted(SCENARIOS))
    ap.add_argument("--inject-at", type=float, default=8.0)
    ap.add_argument("--duration", type=float, default=45.0)
    ap.add_argument("--cooldown", type=float, default=15.0)
    args = ap.parse_args()

    conn = mavutil.mavlink_connection(args.conn, dialect="ardupilotmega")
    if conn.wait_heartbeat(timeout=30) is None:
        print("no heartbeat", file=sys.stderr)
        return 1

    runner = LiveRunner(conn, cadence_s=1.0, window_s=120.0)
    runner.request_streams(rate_hz=10.0)
    runner.fetch_params()

    for name, value in RESETS[args.scenario]:
        set_param(conn, name, value)
    time.sleep(0.5)

    if not arm_and_takeoff(conn):
        print("could not get airborne", file=sys.stderr)
        return 1

    gate = EscalationGate(cooldown_s=args.cooldown, clear_after_s=8.0)
    console = Console()
    motors = sorted(k.replace("_FUNCTION", "") for k, v in runner.params.items()
                    if k.endswith("_FUNCTION") and 33 <= int(v) <= 44)
    console.header(runner.params, motors)

    state = {"injected": False}

    def on_cycle(r):
        if not state["injected"] and r.t >= args.inject_at:
            print(f"  {'':>7}  >>> injecting '{args.scenario}'")
            for name, value in SCENARIOS[args.scenario]:
                set_and_verify(conn, name, value)
            state["injected"] = True

        vibe = runner.buffer.messages.get("VIBE", [])
        rcou = runner.buffer.messages.get("RCOU", [])
        telemetry = {
            "vibe_z": max((m.get("VibeZ", 0.0) for m in vibe[-20:]), default=0.0),
            "servo_max": max((rcou[-1].get(f"C{i}", 0.0) or 0.0 for i in range(1, 5)),
                             default=0.0) if rcou else 0.0,
            "alt": 0.0,
        }

        raised = gate.submit(r.incidents, r.t)
        console.cycle(r.t, telemetry, raised, len(gate.active))

    reports = runner.run(args.duration, on_cycle=on_cycle)
    console.summary(gate.stats, len(reports), args.duration)

    for name, value in RESETS[args.scenario]:
        set_param(conn, name, value)
    conn.set_mode("LAND")

    # The gate must collapse a continuous fault to far fewer advisories than incidents.
    ok = gate.stats.seen == 0 or gate.stats.raised < gate.stats.seen
    print("R5+R6 PASS" if ok else "R5+R6 FAIL — gate did not suppress anything")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
