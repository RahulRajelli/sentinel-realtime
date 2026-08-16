#!/usr/bin/env python3
"""What does ArduPilot ITSELF report when the compass is biased?

    python -u scripts/ardupilot_says.py
    python -u scripts/ardupilot_says.py --out docs/_ardupilot-says.json

WHY THIS EXISTS.

Every claim in this project is of the form "the first alarm is not the fault". The obvious
objection -- and the first one a flight-controller engineer will raise -- is:

    "ArduPilot already tells you when something is wrong. What does yours add?"

That objection deserves evidence, not an assertion. So this injects the same magnetometer offset
used by the compass_offset scenario, and records what the AUTOPILOT broadcasts about it over
MAVLink while it happens:

  * STATUSTEXT          -- the human-readable messages a ground station prints in its message
                           pane. This is what a pilot actually sees.
  * EKF_STATUS_REPORT   -- the navigation filter's own variance numbers, which drive the
                           "EKF variance" warnings and the EKF failsafe.
  * SYS_STATUS          -- the per-sensor health bitmask, source of "Bad Compass Health".

The point is not that ArduPilot is bad at this. It is excellent at what it does: it reports, in
real time, every subsystem whose measurements have gone out of tolerance. What it does not do --
because it is a flight controller and not a diagnostic tool -- is RANK those reports causally. It
tells you the navigation filter is unhappy and that the compass is unhealthy. It does not tell
you which of those two facts explains the other.

That gap is the entire product, and this script is how the claim gets checked rather than
believed. Run it and read the transcript.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from pymavlink import mavutil  # noqa: E402

WSL_DISTRO = "Ubuntu-24.04"
AP = "/root/ardupilot"
INSTANCE = 4                       # away from the scenario runner's instances
PORT = 5760 + 10 * INSTANCE

# The same injections the scenarios use. Not bigger ones chosen to make a point -- if the
# comparison needed a larger fault than the experiment uses, it would not be the same comparison.
#
# `compass_offset` is the original arm and its recorded transcript is committed at
# docs/_ardupilot-says.json. It is the DEFAULT and its behaviour is unchanged: the aircraft stays
# on the ground, because a magnetometer bias raises EKF variance and flips 3D_MAG health whether
# or not it is flying.
#
# `hot_gains_lowd` CANNOT be probed that way and this is the whole reason it needs its own entry.
# The fault is a mistuned attitude loop; it produces nothing at all until the controller is
# actively holding attitude against wind. Injecting these gains on a disarmed aircraft on the
# ground records silence, and reporting that silence as "ArduPilot says nothing about this fault"
# would be a measurement error, not a result. So it flies first.
SCENARIOS = {
    "compass_offset": {
        "inject": {"SIM_MAG1_OFS_X": 400.0, "SIM_MAG1_OFS_Y": 400.0},
        "fly": False,
        "banner": "INJECTING MAGNETOMETER OFFSET",
        "event": "SIM_MAG1_OFS_X/Y = 400 (magnetometer biased)",
    },
    "hot_gains_lowd": {
        "inject": {"ATC_ANG_RLL_P": 30.0, "ATC_ANG_PIT_P": 30.0,
                   "ATC_RAT_RLL_P": 0.90, "ATC_RAT_PIT_P": 0.90,
                   "ATC_RAT_RLL_D": 0.0, "ATC_RAT_PIT_D": 0.0,
                   "INS_GYRO_FILTER": 4.0, "SIM_WIND_SPD": 20.0},
        "fly": True,
        "banner": "INJECTING HOT GAINS + 4 Hz GYRO FILTER + 20 m/s WIND",
        "event": "ATC_ANG_*_P=30, ATC_RAT_*_D=0, INS_GYRO_FILTER=4, SIM_WIND_SPD=20",
    },
}


def launch_sitl() -> subprocess.Popen:
    cmd = (
        f"mkdir -p /root/sitl_says && cd /root/sitl_says && rm -f eeprom.bin && "
        f"{AP}/build/sitl/bin/arducopter -S -I{INSTANCE} --model quad --speedup 1 "
        f"--defaults {AP}/Tools/autotest/default_params/copter.parm "
        f"--home 12.9716,77.5946,900,0"
    )
    return subprocess.Popen(["wsl", "-d", WSL_DISTRO, "--", "bash", "-c", cmd],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def connect(timeout_s: int = 90):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            c = mavutil.mavlink_connection(f"tcp:127.0.0.1:{PORT}", retries=1)
            c.wait_heartbeat(timeout=5)
            return c
        except Exception:
            time.sleep(2)
    raise SystemExit("SITL never came up")


def set_param(conn, name: str, value: float) -> None:
    conn.mav.param_set_send(conn.target_system, conn.target_component,
                            name.encode(), float(value),
                            mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
    time.sleep(0.25)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="docs/_ardupilot-says.json")
    ap.add_argument("--scenario", default="compass_offset", choices=sorted(SCENARIOS),
                    help="which fault to inject. compass_offset is the committed arm")
    ap.add_argument("--settle", type=float, default=12.0, help="seconds before injecting")
    ap.add_argument("--watch", type=float, default=25.0, help="seconds to record after injecting")
    args = ap.parse_args()

    cfg = SCENARIOS[args.scenario]
    INJECT = cfg["inject"]

    print(f"launching SITL instance {INSTANCE} -> tcp:127.0.0.1:{PORT}")
    proc = launch_sitl()
    conn = connect()
    print("heartbeat received")

    # Ask for the streams that carry the autopilot's own diagnostics.
    conn.mav.request_data_stream_send(conn.target_system, conn.target_component,
                                      mavutil.mavlink.MAV_DATA_STREAM_ALL, 10, 1)

    if cfg["fly"]:
        # Reused rather than hand-rolled: a GPS fix alone is not enough to arm, the EKF must reach
        # POS_HORIZ_ABS and home must be set. A hand-written arm loop failed every scenario once
        # already, which is why r7_r8_scenarios owns this.
        sys.path.insert(0, str(_ROOT / "scripts"))
        from r7_r8_scenarios import arm_and_takeoff  # noqa: E402
        print(f"scenario {args.scenario} needs to be FLYING before the fault means anything")
        arm_and_takeoff(conn, alt=10.0)
        print("airborne")

    t_start = time.time()
    events: list[dict] = []
    injected_at = None

    def stamp() -> float:
        return round(time.time() - t_start, 3)

    print(f"settling for {args.settle:.0f}s, recording everything the autopilot says...")
    while True:
        now = time.time() - t_start
        if injected_at is None and now >= args.settle:
            injected_at = stamp()
            print(f"\n--- t={injected_at:.2f}s  {cfg['banner']} ---")
            for k, v in INJECT.items():
                set_param(conn, k, v)
                print(f"    {k} = {v}")
            events.append({"t": injected_at, "kind": "INJECT", "text": cfg["event"]})
        if injected_at is not None and now >= args.settle + args.watch:
            break

        m = conn.recv_match(type=["STATUSTEXT", "EKF_STATUS_REPORT", "SYS_STATUS"],
                            blocking=True, timeout=2)
        if m is None:
            continue
        t = stamp()

        if m.get_type() == "STATUSTEXT":
            txt = m.text.strip() if isinstance(m.text, str) else m.text.decode(errors="replace").strip()
            events.append({"t": t, "kind": "STATUSTEXT", "sev": int(m.severity), "text": txt})
            print(f"  [{t:7.2f}] STATUSTEXT sev={m.severity}  {txt}")

        elif m.get_type() == "EKF_STATUS_REPORT":
            # compass_variance is the autopilot's own view of the magnetometer innovation.
            if m.compass_variance >= 0.5:
                events.append({"t": t, "kind": "EKF", "compass_variance": round(m.compass_variance, 3),
                               "flags": int(m.flags),
                               "text": f"EKF compass_variance={m.compass_variance:.3f}"})
                print(f"  [{t:7.2f}] EKF        compass_variance={m.compass_variance:.3f}")

        elif m.get_type() == "SYS_STATUS":
            mag_bit = mavutil.mavlink.MAV_SYS_STATUS_SENSOR_3D_MAG
            healthy = bool(m.onboard_control_sensors_health & mag_bit)
            prev = next((e for e in reversed(events) if e["kind"] == "MAGHEALTH"), None)
            if prev is None or prev["healthy"] != healthy:
                events.append({"t": t, "kind": "MAGHEALTH", "healthy": healthy,
                               "text": f"3D_MAG health -> {'OK' if healthy else 'UNHEALTHY'}"})
                print(f"  [{t:7.2f}] SYS_STATUS 3D_MAG health -> {'OK' if healthy else 'UNHEALTHY'}")

    try:
        conn.close()
    finally:
        proc.terminate()
        subprocess.run(["wsl", "-d", WSL_DISTRO, "--", "bash", "-c",
                        f"pkill -f 'arducopter -S -I{INSTANCE}' || true"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"scenario": args.scenario, "flew": cfg["fly"],
                               "injected_at": injected_at, "inject": INJECT,
                               "events": events}, indent=1), encoding="utf-8")

    print("\n" + "=" * 78)
    print("WHAT ARDUPILOT SAID")
    print("=" * 78)
    after = [e for e in events if injected_at is not None and e["t"] >= injected_at
             and e["kind"] in ("STATUSTEXT", "EKF", "MAGHEALTH")]
    if not after:
        print("  (nothing) -- the autopilot broadcast no diagnostic about this fault")
    for e in after:
        print(f"  +{e['t'] - injected_at:6.2f}s  {e['kind']:10} {e['text']}")
    print()
    print("  Read the list above and ask the question this project exists for: which of these")
    print("  is the CAUSE, and which are consequences of it? The autopilot does not say, because")
    print("  ranking them is not its job. That is the gap.")
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
