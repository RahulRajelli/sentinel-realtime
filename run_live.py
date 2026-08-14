#!/usr/bin/env python3
"""Run the live detector loop against a MAVLink link (Phase R4).

    python run_live.py --duration 30
    python run_live.py --conn tcp:127.0.0.1:5760 --cadence 1.0

Prints one line per cycle plus a timing summary. The timing summary is the R8 input: it
shows how detector cost scales with buffer size, which is what decides when the E1
streaming refactor becomes necessary rather than merely tidier.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from pymavlink import mavutil

from sentinel.params import ACTUATOR_PARAMS, BATTERY_PARAMS
from sentinel.runner import LiveRunner


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn", default="tcp:127.0.0.1:5760")
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--cadence", type=float, default=1.0)
    ap.add_argument("--window", type=float, default=120.0)
    ap.add_argument("--rate", type=float, default=10.0, help="requested stream rate, Hz")
    args = ap.parse_args()

    print(f"connecting {args.conn} ...")
    conn = mavutil.mavlink_connection(args.conn, dialect="ardupilotmega")
    if conn.wait_heartbeat(timeout=30) is None:
        print("no heartbeat", file=sys.stderr)
        return 1
    print(f"heartbeat from system {conn.target_system}")

    runner = LiveRunner(conn, cadence_s=args.cadence, window_s=args.window)

    print(f"requesting {len(runner.__class__.__mro__) and 13} messages at {args.rate} Hz")
    runner.request_streams(rate_hz=args.rate)

    print("fetching detector thresholds ...")
    params = runner.fetch_params()
    got = {k: params.get(k) for k in ACTUATOR_PARAMS + BATTERY_PARAMS}
    print(f"  params: {got}")
    servo_fns = sorted(k for k, v in params.items()
                       if k.endswith("_FUNCTION") and 33 <= int(v) <= 44)
    print(f"  motor outputs: {[s.replace('_FUNCTION', '') for s in servo_fns] or 'none declared'}")
    if runner.param_cache and not runner.param_cache.complete:
        print("  WARNING: threshold params incomplete -- actuator/battery verdicts unreliable")

    print(f"\nrunning {args.duration:.0f}s at {args.cadence:.1f}s cadence "
          f"(window {args.window:.0f}s)\n")
    print(f"{'t':>6} {'recs':>7} {'msgs':>7} {'build':>7} {'detect':>7}  incidents")
    print("-" * 72)

    def on_cycle(r):
        summary = "none"
        if r.incidents:
            parts = [f"{i.type}[{i.severity}]" for i in r.incidents[:3]]
            if len(r.incidents) > 3:
                parts.append(f"+{len(r.incidents) - 3}")
            summary = " ".join(parts)
        print(f"{r.t:6.1f} {r.buffer_records:7d} {r.messages_in:7d} "
              f"{r.build_ms:6.1f}ms {r.detect_ms:6.1f}ms  {summary}")

    reports = runner.run(args.duration, on_cycle=on_cycle)

    if not reports:
        print("no cycles ran")
        return 1

    print("\n--- timing (R8 input) ---")
    first, last = reports[0], reports[-1]
    print(f"cycles              : {len(reports)}")
    print(f"buffer growth       : {first.buffer_records} -> {last.buffer_records} records")
    print(f"build ParsedLog     : {first.build_ms:.1f} -> {last.build_ms:.1f} ms")
    print(f"run 7 detectors     : {first.detect_ms:.1f} -> {last.detect_ms:.1f} ms")
    worst = max(r.total_ms for r in reports)
    budget = args.cadence * 1000.0
    print(f"worst cycle         : {worst:.1f} ms of a {budget:.0f} ms budget "
          f"({worst / budget:.1%})")
    slowest = sorted(last.per_detector_ms.items(), key=lambda kv: -kv[1])[:3]
    print(f"slowest detectors   : {[(n, round(v, 1)) for n, v in slowest]}")

    total_incidents = sum(len(r.incidents) for r in reports)
    cycles_with = sum(1 for r in reports if r.incidents)
    print(f"\nincidents           : {total_incidents} across {cycles_with}/{len(reports)} cycles")
    if runner.adapter.unmapped:
        top = sorted(runner.adapter.unmapped.items(), key=lambda kv: -kv[1])[:5]
        print(f"unmapped messages   : {top}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
