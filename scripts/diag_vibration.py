#!/usr/bin/env python3
"""Isolate: does SITL actually produce vibration, and do we see it?

Two runs failed to detect an injected vibration fault. Readback proved the parameter was
applied, so the question is whether the simulator generated anything. This separates
"the sim never vibrated" from "the detector missed it" by printing raw VIBRATION values
either side of the injection -- the same distinction the R8 gates depend on.

SIM_VIB_MOT_MASK defaults to 0 on this firmware, and it is a bitmask of which motors
generate vibration. With no bits set, SIM_VIB_MOT_MAX has nothing to act on.
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymavlink import mavutil


def set_param(conn, name, value):
    conn.mav.param_set_send(
        conn.target_system, conn.target_component,
        name.encode("utf-8"), float(value), mavutil.mavlink.MAV_PARAM_TYPE_REAL32)
    time.sleep(0.3)


def read_param(conn, name, timeout=4.0):
    conn.mav.param_request_read_send(
        conn.target_system, conn.target_component, name.encode("utf-8"), -1)
    end = time.time() + timeout
    while time.time() < end:
        m = conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=1.0)
        if m is None:
            continue
        pid = m.param_id.decode("utf-8", "ignore") if isinstance(m.param_id, bytes) else m.param_id
        if pid.rstrip("\x00") == name:
            return float(m.param_value)
    return None


def sample(conn, seconds, label):
    """Collect raw VIBRATION for a while and summarise."""
    vals = {"x": [], "y": [], "z": []}
    clip = 0
    servo_max = 0
    end = time.time() + seconds
    while time.time() < end:
        m = conn.recv_match(blocking=True, timeout=1.0)
        if m is None:
            continue
        t = m.get_type()
        if t == "VIBRATION":
            vals["x"].append(m.vibration_x)
            vals["y"].append(m.vibration_y)
            vals["z"].append(m.vibration_z)
            clip = max(clip, m.clipping_0)
        elif t == "SERVO_OUTPUT_RAW":
            servo_max = max(servo_max, m.servo1_raw, m.servo2_raw, m.servo3_raw, m.servo4_raw)
    n = len(vals["z"])
    if not n:
        print(f"  {label:22s} NO VIBRATION MESSAGES")
        return
    print(f"  {label:22s} n={n:3d}  VibeZ mean={statistics.mean(vals['z']):7.2f} "
          f"max={max(vals['z']):7.2f}  VibeX max={max(vals['x']):6.2f}  "
          f"clip0={clip}  servo_max={servo_max}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn", default="tcp:127.0.0.1:5760")
    args = ap.parse_args()

    conn = mavutil.mavlink_connection(args.conn, dialect="ardupilotmega")
    if conn.wait_heartbeat(timeout=30) is None:
        print("no heartbeat")
        return 1

    for mid in (241, 36, 30):  # VIBRATION, SERVO_OUTPUT_RAW, ATTITUDE
        conn.mav.command_long_send(
            conn.target_system, conn.target_component,
            mavutil.mavlink.MAV_CMD_SET_MESSAGE_INTERVAL, 0, mid, 100000, 0, 0, 0, 0, 0)
        time.sleep(0.05)

    print("current vibration params:")
    for p in ("SIM_VIB_MOT_MAX", "SIM_VIB_MOT_MASK", "SIM_VIB_MOT_MULT",
              "SIM_VIB_FREQ_X", "SIM_VIB_FREQ_Y", "SIM_VIB_FREQ_Z", "SIM_ACC1_RND"):
        print(f"  {p:20s} {read_param(conn, p)}")

    print("\nbaseline (motors as-is):")
    sample(conn, 6, "before injection")

    print("\nsetting mask + amplitude + frequency:")
    set_param(conn, "SIM_VIB_MOT_MASK", 15)   # motors 1-4
    set_param(conn, "SIM_VIB_MOT_MAX", 80)
    set_param(conn, "SIM_VIB_FREQ_X", 50)
    set_param(conn, "SIM_VIB_FREQ_Y", 50)
    set_param(conn, "SIM_VIB_FREQ_Z", 50)
    for p in ("SIM_VIB_MOT_MASK", "SIM_VIB_MOT_MAX", "SIM_VIB_FREQ_Z"):
        print(f"  readback {p:20s} {read_param(conn, p)}")

    print("\nafter injection:")
    sample(conn, 6, "t+0-6s")
    sample(conn, 6, "t+6-12s")

    print("\nalso trying accelerometer noise:")
    set_param(conn, "SIM_ACC1_RND", 50)
    sample(conn, 6, "with SIM_ACC1_RND=50")

    print("\nresetting")
    for p, v in (("SIM_VIB_MOT_MASK", 0), ("SIM_VIB_MOT_MAX", 0), ("SIM_ACC1_RND", 0),
                 ("SIM_VIB_FREQ_X", 0), ("SIM_VIB_FREQ_Y", 0), ("SIM_VIB_FREQ_Z", 0)):
        set_param(conn, p, v)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
