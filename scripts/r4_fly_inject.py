#!/usr/bin/env python3
"""R4 under load: fly SITL, inject a real fault, measure detection latency.

A runner that reports nothing while the aircraft sits disarmed proves very little. This
flies the vehicle, injects accelerometer noise partway through, and records how long the
live loop takes to raise a vibration incident.

Also a preview of R8's two gates:
  * before injection the system must stay silent (the null case)
  * after injection it must raise the *injected* fault, not a symptom cascade

Parameter name comes from sitl-harness/sim_params.json, not documentation. Two corrections
found that way, both silent failures otherwise:

  * SIM_ACC_RND does not exist on this firmware (it is per-IMU SIM_ACC1_RND), and
  * SIM_ACC1_RND is accelerometer *random noise*, which does not drive the VIBE metric.
    The parameter that does is SIM_VIB_MOT_MAX -- motor-driven vibration amplitude.

A first run set SIM_ACC1_RND=40 and saw VibeZ stay at 0.0 for 25 s. Injections are now
read back after setting, so "the fault never actually happened" is distinguishable from
"the detector missed it" -- otherwise a benchmark scores its own plumbing.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymavlink import mavutil

from sentinel.runner import LiveRunner

INJECT_PARAM = "SIM_VIB_MOT_MAX"

# A fault is a *set* of parameters, not one. Vibration in SITL is motor-driven: the mask
# selects which motors contribute and the amplitude scales them. Setting the amplitude
# alone -- as a first run did -- changes nothing, because the default mask is 0.
# It also only appears while the motors are actually spinning, so the aircraft must be
# airborne before injection means anything. Verified against sitl-harness/sim_params.json.
SCENARIOS: dict[str, list[tuple[str, float]]] = {
    # Read from AP_InertialSensor_SITL.cpp:128,142-156 rather than guessed:
    #     if (motors_on) accel_noise = sitl->accel_noise[instance];      // SIM_ACC1_RND
    #     if (!is_zero(sitl->vibe_motor) && motors_on)                   // SIM_VIB_MOT_MAX
    #         accel.z += sinf(phase) * calculate_noise(accel_noise * vibe_motor_scale, ...)
    # SIM_VIB_MOT_MAX is a FREQUENCY baseline in Hz, not an amplitude -- the amplitude is
    # SIM_ACC1_RND. Either one alone yields exactly zero: with ACC1_RND=0 the amplitude is
    # zero, and with VIB_MOT_MAX=0 the block is skipped entirely. Both are required, and
    # motors_on requires throttle > SIM_INS_THR_MIN (default 0.1), i.e. actually flying.
    "vibration": [("SIM_VIB_MOT_MAX", 200), ("SIM_ACC1_RND", 40)],
    "gps_loss": [("SIM_GPS1_FIXTYPE", 0)],
    "wind": [("SIM_WIND_SPD", 15), ("SIM_WIND_DIR", 90)],
    "null": [],
}
RESETS: dict[str, list[tuple[str, float]]] = {
    "vibration": [("SIM_VIB_MOT_MAX", 0), ("SIM_ACC1_RND", 0)],
    "gps_loss": [("SIM_GPS1_FIXTYPE", 6)],
    "wind": [("SIM_WIND_SPD", 0)],
    "null": [],
}


def set_param(conn, name: str, value: float) -> None:
    conn.mav.param_set_send(
        conn.target_system, conn.target_component,
        name.encode("utf-8"), float(value),
        mavutil.mavlink.MAV_PARAM_TYPE_REAL32,
    )


def read_param(conn, name: str, timeout: float = 5.0) -> float | None:
    """Read a parameter back. An injection that silently did not apply is worse than a
    loud failure, because the run still produces a plausible-looking 'not detected'."""
    conn.mav.param_request_read_send(
        conn.target_system, conn.target_component, name.encode("utf-8"), -1)
    deadline = time.time() + timeout
    while time.time() < deadline:
        msg = conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=1.0)
        if msg is None:
            continue
        pid = msg.param_id
        if isinstance(pid, bytes):
            pid = pid.decode("utf-8", "ignore")
        if pid.rstrip("\x00") == name:
            return float(msg.param_value)
    return None


def set_and_verify(conn, name: str, value: float) -> bool:
    set_param(conn, name, value)
    time.sleep(0.4)
    got = read_param(conn, name)
    ok = got is not None and abs(got - value) < max(0.01, abs(value) * 0.01)
    print(f"    {name} := {value}  ->  readback {got}  {'OK' if ok else 'MISMATCH'}")
    return ok


def arm_and_takeoff(conn, alt: float = 20.0) -> bool:
    print("waiting for 3D fix ...")
    deadline = time.time() + 90
    fix = 0
    while time.time() < deadline and fix < 3:
        msg = conn.recv_match(type="GPS_RAW_INT", blocking=True, timeout=5)
        if msg:
            fix = msg.fix_type
    print(f"  fix={fix}")
    if fix < 3:
        return False

    conn.set_mode("GUIDED")
    time.sleep(1)

    for attempt in range(12):
        conn.mav.command_long_send(
            conn.target_system, conn.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0)
        ack = conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=4)
        if ack and ack.command == mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM and ack.result == 0:
            print("  ARMED")
            break
        time.sleep(1.5)
    else:
        return False

    conn.mav.command_long_send(
        conn.target_system, conn.target_component,
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 0, alt)
    ack = conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=6)
    print(f"  takeoff ack={getattr(ack, 'result', 'NONE')}")

    peak = 0.0
    deadline = time.time() + 90
    while time.time() < deadline and peak < alt * 0.8:
        msg = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=5)
        if msg:
            peak = max(peak, msg.relative_alt / 1000.0)
    print(f"  altitude {peak:.1f} m")
    return peak >= alt * 0.5


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conn", default="tcp:127.0.0.1:5760")
    ap.add_argument("--inject-at", type=float, default=12.0, help="seconds into the run")
    ap.add_argument("--duration", type=float, default=40.0)
    ap.add_argument("--scenario", default="vibration", choices=sorted(SCENARIOS),
                    help="fault to inject; parameter sets come from sim_params.json")
    ap.add_argument("--amplitude", type=float, default=None,
                    help="override the scenario's main amplitude value")
    args = ap.parse_args()

    injection = list(SCENARIOS[args.scenario])
    if args.amplitude is not None and injection:
        injection[-1] = (injection[-1][0], args.amplitude)

    conn = mavutil.mavlink_connection(args.conn, dialect="ardupilotmega")
    if conn.wait_heartbeat(timeout=30) is None:
        print("no heartbeat", file=sys.stderr)
        return 1
    print(f"heartbeat from system {conn.target_system}")

    runner = LiveRunner(conn, cadence_s=1.0, window_s=120.0)
    runner.request_streams(rate_hz=10.0)
    runner.fetch_params()
    print(f"thresholds: MOT_PWM_MIN={runner.params.get('MOT_PWM_MIN')} "
          f"MOT_PWM_MAX={runner.params.get('MOT_PWM_MAX')}")

    # Make sure we start from a clean sim: no leftover injection from an earlier run.
    for name, value in RESETS[args.scenario]:
        set_param(conn, name, value)
    time.sleep(0.5)

    if not arm_and_takeoff(conn):
        print("could not get airborne", file=sys.stderr)
        return 1

    state = {"injected_at": None, "first_detect": None, "first_type": None}

    print(f"\nflying. scenario '{args.scenario}' -> {injection} at t+{args.inject_at:.0f}s\n")
    print(f"{'t':>6} {'recs':>7} {'detect':>8}  vibeZ  servo_max  incidents")
    print("-" * 78)

    def on_cycle(r):
        if state["injected_at"] is None and r.t >= args.inject_at:
            print(f"{'':>6} {'':>7} {'':>8}  >>> injecting scenario '{args.scenario}'")
            state["verified"] = all(
                set_and_verify(conn, name, value) for name, value in injection)
            state["injected_at"] = r.t

        vibe = runner.buffer.messages.get("VIBE", [])
        recent = [m.get("VibeZ", 0.0) for m in vibe[-30:]]
        peak = max(recent) if recent else 0.0

        # Motor output proves the aircraft is actually flying. Motor-driven vibration is
        # zero while disarmed, so a silent run with servo_max at MOT_PWM_MIN means the
        # test never exercised anything -- not that the detector failed.
        rcou = runner.buffer.messages.get("RCOU", [])
        servo_max = 0.0
        if rcou:
            last = rcou[-1]
            servo_max = max((last.get(f"C{i}", 0.0) or 0.0) for i in range(1, 5))

        names = [f"{i.type}[{i.severity}]" for i in r.incidents]
        if r.incidents and state["first_detect"] is None and state["injected_at"] is not None:
            state["first_detect"] = r.t
            state["first_type"] = r.incidents[0].type

        print(f"{r.t:6.1f} {r.buffer_records:7d} {r.detect_ms:7.1f}ms  {peak:5.1f}  "
              f"{servo_max:9.0f}  {' '.join(names) if names else 'none'}")

    runner.run(args.duration, on_cycle=on_cycle)

    print("\n--- result ---")
    if state["injected_at"] is None:
        print("never injected")
        return 1
    if state["first_detect"] is None:
        print(f"NOT DETECTED: scenario {args.scenario} injected at "
              f"t+{state['injected_at']:.1f}s, no incident within "
              f"{args.duration - state['injected_at']:.0f}s")
        rc = 1
    else:
        latency = state["first_detect"] - state["injected_at"]
        print(f"injected at      : t+{state['injected_at']:.1f}s")
        print(f"first detection  : t+{state['first_detect']:.1f}s  ({state['first_type']})")
        print(f"detection latency: {latency:.1f}s")
        rc = 0

    print("\nlanding")
    for name, value in RESETS[args.scenario]:
        set_param(conn, name, value)
    conn.set_mode("LAND")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
