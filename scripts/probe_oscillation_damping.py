#!/usr/bin/env python3
"""Probe: can reduced rate-loop DAMPING push tracking error past the oscillation threshold?

    python -u scripts/probe_oscillation_damping.py
    python -u scripts/probe_oscillation_damping.py --configs D1,D3

Justification, prediction and falsifier were written and committed BEFORE this ran --
see docs/probe-hot-gains-damping.md. Read that first; this script only measures.

WHAT IT MEASURES, and why exactly these two numbers.

`control_oscillation` (oscillation.py) requires, per 1.5 s window:

    max|desired - actual| >= 3.0 deg      (OSCILLATION_AMPLITUDE_DEG)
    zero crossings       >= 3.5 /s        (ZERO_CROSSINGS_PER_SEC_MIN)

over >= 2 consecutive windows (MIN_SUSTAINED_WINDOWS). This computes exactly those, from
ATTITUDE (actual) and ATTITUDE_TARGET (desired) at 10 Hz, so the output is directly comparable to
the eight probes already recorded in r7_r8_scenarios.py rather than to some proxy.

It does NOT capture a RunBundle and does NOT touch the detector. The bar stays at 3.0 deg; the
question is only whether the airframe can be made to cross it honestly.

Eight prior probes varied proportional gain and wind and peaked at 2.44 deg. None varied the
derivative term. P sets bandwidth, D sets damping; the recorded reason those probes failed
("raising gains makes the controller track more tightly") rules out getting amplitude by tracking
harder, and says nothing about getting it by damping less. That is the gap this fills.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from pymavlink import mavutil  # noqa: E402

# Proven helpers. Rewriting arming here produced "FAILED to arm/climb" on the first config --
# exactly the failure r7_r8_scenarios.arm_and_takeoff's docstring warns about (a GPS fix alone is
# not enough; the EKF must reach absolute horizontal position and home must be set). Importing it
# also means a future fix to arming lands here for free.
from scripts.r7_r8_scenarios import (  # noqa: E402
    arm_and_takeoff as _arm_and_takeoff,
    launch_sitl as _launch_sitl,
    set_param as _set_param,
)

WSL_DISTRO = "Ubuntu-24.04"
AP = "/root/ardupilot"
INSTANCE = 6
PORT = 5760 + 10 * INSTANCE

WINDOW_S = 1.5            # oscillation.py WINDOW_SIZE_S
AMP_DEG = 3.0             # oscillation.py OSCILLATION_AMPLITUDE_DEG
ZC_PER_S = 3.5            # oscillation.py ZERO_CROSSINGS_PER_SEC_MIN
MIN_WINDOWS = 2           # oscillation.py MIN_SUSTAINED_WINDOWS

# Held at the measured best disturbance (wind 20) and moderate gains from the probe table, so the
# only thing varying across configs is damping. A sweep that moved several levers at once could
# not attribute whatever it found.
BASE = {"ATC_ANG_RLL_P": 30.0, "ATC_ANG_PIT_P": 30.0,
        "ATC_RAT_RLL_P": 0.90, "ATC_RAT_PIT_P": 0.90,
        "SIM_WIND_SPD": 20.0}

CONFIGS = {
    "D1": {"ATC_RAT_RLL_D": 0.0036, "ATC_RAT_PIT_D": 0.0036},                        # default
    "D2": {"ATC_RAT_RLL_D": 0.0010, "ATC_RAT_PIT_D": 0.0010},                        # light
    "D3": {"ATC_RAT_RLL_D": 0.0000, "ATC_RAT_PIT_D": 0.0000},                        # undamped
    "D4": {"ATC_RAT_RLL_D": 0.0000, "ATC_RAT_PIT_D": 0.0000, "INS_GYRO_FILTER": 4.0},  # + lag
}


def launch():
    return _launch_sitl(INSTANCE)


def kill():
    subprocess.run(["wsl", "-d", WSL_DISTRO, "--", "bash", "-c",
                    f"pkill -f 'arducopter -S -I{INSTANCE}' || true"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def connect(timeout_s=90):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            c = mavutil.mavlink_connection(f"tcp:127.0.0.1:{PORT}", retries=1)
            c.wait_heartbeat(timeout=5)
            return c
        except Exception:
            time.sleep(2)
    raise SystemExit("SITL never came up")


def setp(conn, name, value):
    _set_param(conn, name, value)


def windows(samples):
    """Split into 1.5s windows; return per-window (max_amp_deg, zero_crossings_per_s)."""
    if not samples:
        return []
    out, t0, buf = [], samples[0][0], []
    for t, err in samples:
        if t - t0 >= WINDOW_S:
            if len(buf) >= 5:
                amp = max(abs(e) for e in buf)
                zc = sum(1 for a, b in zip(buf, buf[1:]) if (a > 0) != (b > 0))
                out.append((amp, zc / WINDOW_S))
            t0, buf = t, []
        buf.append(err)
    return out


def run_config(name: str, overrides: dict, seconds: float) -> dict:
    print(f"\n{'='*76}\nCONFIG {name}: {overrides}\n{'='*76}")
    proc = launch()
    try:
        conn = connect()
        conn.mav.request_data_stream_send(conn.target_system, conn.target_component,
                                          mavutil.mavlink.MAV_DATA_STREAM_ALL, 10, 1)
        # Gains first, then damping, then wind. Wind last so the airframe is already
        # mis-tuned when the disturbance arrives rather than settling into it.
        for k, v in BASE.items():
            if k != "SIM_WIND_SPD":
                setp(conn, k, v)
        for k, v in overrides.items():
            setp(conn, k, v)
        ok, why = _arm_and_takeoff(conn, alt=10.0)
        if not ok:
            # Report the autopilot's own pre-arm reason rather than a bare failure -- that string
            # is the difference between "my harness is wrong" and "this config cannot fly".
            print(f"  FAILED to arm/climb: {why}")
            return {"config": name, "ok": False, "reason": why}
        setp(conn, "SIM_WIND_SPD", BASE["SIM_WIND_SPD"])
        print(f"  flying; wind {BASE['SIM_WIND_SPD']} m/s; sampling {seconds:.0f}s")

        # DESIRED comes from NAV_CONTROLLER_OUTPUT, not ATTITUDE_TARGET. Measured on this build:
        # over 15 s airborne, ATTITUDE_TARGET arrived 0 times and NAV_CONTROLLER_OUTPUT 153. The
        # first version read ATTITUDE_TARGET quaternions and therefore never took a single sample,
        # reporting 0.00 deg -- which looks exactly like the falsification this probe predicted.
        # nav_roll/nav_pitch are already degrees, so the quaternion conversion goes too.
        des = {"roll": None, "pitch": None}
        samples, t0 = [], time.time()
        while time.time() - t0 < seconds:
            m = conn.recv_match(type=["ATTITUDE", "NAV_CONTROLLER_OUTPUT"], blocking=True, timeout=2)
            if m is None:
                continue
            if m.get_type() == "NAV_CONTROLLER_OUTPUT":
                des["roll"] = float(m.nav_roll)
                des["pitch"] = float(m.nav_pitch)
            elif m.get_type() == "ATTITUDE" and des["roll"] is not None:
                er = math.degrees(m.roll) - des["roll"]
                ep = math.degrees(m.pitch) - des["pitch"]
                samples.append((time.time() - t0, er if abs(er) >= abs(ep) else ep))

        if len(samples) < 20:
            # Refuse to report a number from an empty measurement. 0.00 deg from 0 samples is
            # indistinguishable in the results table from 0.00 deg genuinely measured.
            print(f"  NO DATA: {len(samples)} samples -- measurement broken, not a result")
            return {"config": name, "ok": False, "reason": f"only {len(samples)} samples"}

        w = windows(samples)
        qual = [(a, z) for a, z in w if a >= AMP_DEG and z >= ZC_PER_S]
        run = best = 0
        for a, z in w:
            run = run + 1 if (a >= AMP_DEG and z >= ZC_PER_S) else 0
            best = max(best, run)
        res = {"config": name, "ok": True, "overrides": overrides,
               "samples": len(samples), "windows": len(w),
               "best_amp_deg": round(max((a for a, _ in w), default=0.0), 2),
               "best_zc_per_s": round(max((z for _, z in w), default=0.0), 2),
               "qualifying_windows": len(qual),
               "longest_consecutive": best,
               "would_fire": best >= MIN_WINDOWS}
        print(f"  best amp {res['best_amp_deg']:.2f} deg   best zc {res['best_zc_per_s']:.2f}/s"
              f"   qualifying {res['qualifying_windows']}   consecutive {best}"
              f"   -> {'FIRES' if res['would_fire'] else 'does not fire'}")
        conn.close()
        return res
    finally:
        proc.terminate()
        kill()
        time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default="D1,D2,D3,D4")
    ap.add_argument("--seconds", type=float, default=30.0)
    ap.add_argument("--out", default="probe_oscillation_damping.json")
    args = ap.parse_args()

    names = [c.strip() for c in args.configs.split(",") if c.strip()]
    results = [run_config(n, CONFIGS[n], args.seconds) for n in names if n in CONFIGS]

    print("\n" + "=" * 76)
    print("DAMPING SWEEP  (threshold: amp >= 3.0 deg AND zc >= 3.5/s, 2 consecutive windows)")
    print("=" * 76)
    print(f"{'config':7} {'D gain':>9} {'best amp':>9} {'best zc':>9} {'qual':>5} {'consec':>7}  verdict")
    for r in results:
        if not r.get("ok"):
            print(f"{r['config']:7} {'-':>9} {'-':>9} {'-':>9} {'-':>5} {'-':>7}  {r['reason']}")
            continue
        d = r["overrides"].get("ATC_RAT_RLL_D", 0.0)
        print(f"{r['config']:7} {d:>9.4f} {r['best_amp_deg']:>9.2f} {r['best_zc_per_s']:>9.2f} "
              f"{r['qualifying_windows']:>5} {r['longest_consecutive']:>7}  "
              f"{'FIRES' if r['would_fire'] else 'no'}")
    print(f"\n  prior best across 8 gain/wind probes: 2.44 deg (ang30 rat0.90 wind20)")
    fired = [r for r in results if r.get("would_fire")]
    if fired:
        print(f"  {len(fired)} config(s) would fire control_oscillation -- pair C is reachable.")
    else:
        print("  FALSIFIED as predicted in docs/probe-hot-gains-damping.md: damping is not the")
        print("  binding constraint. Record pair C as unreachable by disturbance methods.")
        print("  Do NOT lower OSCILLATION_AMPLITUDE_DEG.")
    Path(args.out).write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
