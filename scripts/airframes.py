#!/usr/bin/env python3
"""Same fault, different airframes -- does redundancy change what the detectors see?

    python scripts/airframes.py --airframes quad,hexa,octa --fault motor_fail

The R8 scenarios all fly a quad. That is a fine control for detector work and a poor one for
anyone deciding whether this is useful on *their* aircraft, because the interesting property of
a fault is not what it does to a sensor -- it is what the airframe does about it.

Motor-out is the case that makes the point. A quad has no yaw authority to spare: lose one motor
and it departs. A hexa has redundancy: it keeps flying, degraded, with the surviving motors
picking up the load. **The injection is identical. The aircraft's response is not, and neither is
the advisory stream.** For a logistics operator carrying cargo, that difference is the whole
question.

Injection uses `SIM_ENGINE_FAIL` (a bitmask of motors) with `SIM_ENGINE_MUL` (thrust multiplier),
so "motor 1 produces no thrust" is one parameter pair and is readback-verified like every other
injection in this project.

Honest limits, stated because a reader will ask:
  * Only `arducopter` is built here, so **fixed-wing VTOL (quadplane) is not covered**. The
    `tilt` / `tilthvec` frames are tiltrotor copters -- a VTOL *configuration*, not a quadplane.
    A real quadplane needs `arduplane` compiled; the harness would otherwise be unchanged.
  * Every frame flown is reported with whether it actually booted and armed. A frame that failed
    to launch is shown as such rather than dropped, because a missing row and a clean row look
    identical in a table.
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
_FLIGHTDX_SRC = _ROOT.parent / "ardupilot-log-analyzer" / "src"
if _FLIGHTDX_SRC.exists():
    sys.path.insert(1, str(_FLIGHTDX_SRC))

from pymavlink import mavutil

from scripts.r7_r8_scenarios import (
    WSL_DISTRO,
    AP,
    arm_and_takeoff,
    port_for_instance,
    read_param,
    set_param,
)
from sentinel.bundle import InjectedParam
from sentinel.capture import BundleRecorder, metrics_from_result
from sentinel.gate import EscalationGate
from sentinel.runner import LiveRunner

# `--model` is the physics frame; `--defaults` is the parameter set that tells the flight code
# which frame class it is. Both must match or the aircraft flies a mixer it was not tuned for.
# Verified present on this machine 2026-08-14; a frame absent from either list is not flown.
AIRFRAMES: dict[str, dict] = {
    "quad": {
        "model": "quad", "defaults": "copter.parm", "motors": 4,
        "note": "baseline. No spare yaw authority -- a motor-out is not survivable."},
    "hexa": {
        "model": "hexa", "defaults": "copter-hexa.parm", "motors": 6,
        "note": "logistics / cargo. Redundant: should keep flying on 5 motors."},
    "octa": {
        "model": "octa", "defaults": "copter-octa.parm", "motors": 8,
        "note": "heavy lift. Most redundancy, least degradation."},
    "coax": {
        "model": "coax", "defaults": "copter-coax.parm", "motors": 2,
        "note": "coaxial. Two motors plus control surfaces -- a different actuator topology."},
    "tilt": {
        "model": "tilthvec", "defaults": "copter.parm", "motors": 4,
        "note": "tiltrotor VTOL configuration. NOT a quadplane -- arduplane is not built here."},
}

FAULTS: dict[str, dict] = {
    "motor_fail": {
        "inject": [("SIM_ENGINE_FAIL", 1.0), ("SIM_ENGINE_MUL", 0.0)],
        "note": "motor 1 produces zero thrust from t+inject onward"},
    "vibration": {
        "inject": [("SIM_VIB_MOT_MAX", 200.0), ("SIM_ACC1_RND", 40.0)],
        "note": "airframe vibration, identical injection across frames"},
    "null": {"inject": [], "note": "nothing injected -- the hallucination control"},
}


def launch(model: str, defaults: str, instance: int) -> subprocess.Popen:
    cmd = (
        f"mkdir -p /root/sitl_a{instance} && cd /root/sitl_a{instance} && rm -f eeprom.bin && "
        f"{AP}/build/sitl/bin/arducopter -S -I{instance} --model {model} --speedup 1 "
        f"--defaults {AP}/Tools/autotest/default_params/{defaults} "
        f"--home 12.9716,77.5946,900,0")
    return subprocess.Popen(["wsl", "-d", WSL_DISTRO, "--", "bash", "-c", cmd],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def fly(name: str, cfg: dict, fault: str, instance: int,
        inject_at: float, duration: float, bundles_dir: Path | None) -> dict:
    port = port_for_instance(instance)
    fcfg = FAULTS[fault]
    print(f"\n{'=' * 78}\nAIRFRAME: {name}  ({cfg['motors']} motors)  |  fault: {fault}")
    print(f"  {cfg['note']}")
    print(f"  model={cfg['model']}  defaults={cfg['defaults']}  instance {instance} "
          f"-> tcp:127.0.0.1:{port}\n{'=' * 78}")

    result = {"airframe": name, "motors": cfg["motors"], "fault": fault,
              "model": cfg["model"], "booted": False, "armed": False, "ok": False}
    proc = launch(cfg["model"], cfg["defaults"], instance)

    try:
        time.sleep(6)
        conn = None
        for _ in range(40):
            try:
                conn = mavutil.mavlink_connection(f"tcp:127.0.0.1:{port}",
                                                  dialect="ardupilotmega")
                break
            except Exception:
                time.sleep(1)
        if conn is None or conn.wait_heartbeat(timeout=60) is None:
            result["error"] = "no heartbeat -- frame likely unsupported by this build"
            print(f"  FAILED: {result['error']}")
            return result
        result["booted"] = True

        runner = LiveRunner(conn, cadence_s=1.0, window_s=120.0)
        runner.request_streams(rate_hz=10.0)
        runner.fetch_params()

        ok, why = arm_and_takeoff(conn)
        print(f"  takeoff: {why}")
        result["armed"] = ok
        if not ok:
            result["error"] = why
            return result

        gate = EscalationGate(cooldown_s=20.0, clear_after_s=8.0)
        recorder = BundleRecorder(scenario=f"{name}_{fault}",
                                  note=f"{cfg['note']} | {fcfg['note']}")
        state = {"t_inject": None, "verified": False, "first": None, "first_t": None}
        advisories: list[dict] = []

        def on_cycle(r):
            if state["t_inject"] is None and r.t >= inject_at:
                good, applied = True, []
                for pname, pval in fcfg["inject"]:
                    set_param(conn, pname, pval)
                    time.sleep(0.3)
                    back = read_param(conn, pname)
                    ok_one = back is not None and abs(back - pval) < max(0.01, abs(pval) * 0.01)
                    good &= ok_one
                    applied.append(InjectedParam(name=pname, value=pval, readback=back,
                                                 applied=bool(ok_one)))
                    print(f"    inject {pname}={pval} -> readback {back}")
                state["verified"] = good or not fcfg["inject"]
                state["t_inject"] = r.t
                recorder.on_injection(applied, r.t, state["verified"])

            for adv in gate.submit(r.incidents, r.t):
                pre = state["t_inject"] is None
                recorder.on_advisory(r.t, adv.incident, adv.reason, pre_inject=pre)
                advisories.append({"t": round(r.t, 1), "type": adv.incident.type,
                                   "severity": adv.incident.severity, "pre": pre})
                if not pre and state["first"] is None:
                    state["first"] = adv.incident.type
                    state["first_t"] = r.t
                print(f"  {r.t:6.1f}s  {'PRE ' if pre else ''}{adv.incident.type}"
                      f"[{adv.incident.severity}]")
            recorder.on_cycle(r)

        reports = runner.run(duration, on_cycle=on_cycle)

        latency = (state["first_t"] - state["t_inject"]
                   if state["first_t"] is not None and state["t_inject"] is not None else None)
        result.update({
            "ok": True,
            "inject_verified": state["verified"],
            "first_advised": state["first"],
            "latency_s": round(latency, 1) if latency is not None else None,
            "all_advised": sorted({a["type"] for a in advisories if not a["pre"]}),
            "pre_inject": sum(1 for a in advisories if a["pre"]),
            "incidents": gate.stats.seen,
            "advisories": gate.stats.raised,
            "suppression": round(gate.stats.suppression_ratio, 3),
            "cycles": len(reports),
            "buffer_first": reports[0].buffer_records if reports else 0,
            "buffer_last": reports[-1].buffer_records if reports else 0,
            "detect_ms_first": round(reports[0].detect_ms, 2) if reports else 0.0,
            "detect_ms_last": round(reports[-1].detect_ms, 2) if reports else 0.0,
            "worst_cycle_ms": round(max((r.total_ms for r in reports), default=0.0), 1),
        })

        if bundles_dir is not None:
            bundle = recorder.finish(runner.params, metrics_from_result(result))
            p = bundle.save(Path(bundles_dir) / f"{name}_{fault}.json")
            result["bundle"] = str(p)
            print(f"  bundle: {p.name}  id={bundle.bundle_id}")

        conn.set_mode("LAND")
        return result

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        subprocess.run(["wsl", "-d", WSL_DISTRO, "--", "bash", "-c",
                        f"pkill -f 'I{instance} --model' || true"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--airframes", default="quad,hexa,octa")
    ap.add_argument("--fault", default="motor_fail", choices=sorted(FAULTS))
    ap.add_argument("--inject-at", type=float, default=10.0)
    ap.add_argument("--duration", type=float, default=35.0)
    ap.add_argument("--bundles", default="bundles")
    ap.add_argument("--out", default="airframe_results.json")
    # Instances 2-5 are used by r7_r8_scenarios; start clear of them so both can run.
    ap.add_argument("--first-instance", type=int, default=6)
    args = ap.parse_args()

    names = [a.strip() for a in args.airframes.split(",") if a.strip()]
    unknown = [n for n in names if n not in AIRFRAMES]
    if unknown:
        print(f"unknown airframes: {unknown}; have {sorted(AIRFRAMES)}", file=sys.stderr)
        return 1

    results = [fly(n, AIRFRAMES[n], args.fault, args.first_instance + i,
                   args.inject_at, args.duration, Path(args.bundles))
               for i, n in enumerate(names)]

    print(f"\n\n{'=' * 100}\nSAME FAULT ({args.fault}), DIFFERENT AIRFRAMES\n{'=' * 100}")
    hdr = (f"{'airframe':<10}{'mot':>4}  {'booted':<7}{'armed':<7}"
           f"{'first advisory':<24}{'lat':>6}{'adv':>5}{'supp':>7}  all advised")
    print(hdr + "\n" + "-" * 100)
    for r in results:
        lat = f"{r['latency_s']}s" if r.get("latency_s") is not None else "-"
        print(f"{r['airframe']:<10}{r['motors']:>4}  "
              f"{'yes' if r['booted'] else 'NO':<7}{'yes' if r['armed'] else 'NO':<7}"
              f"{str(r.get('first_advised') or '-'):<24}{lat:>6}"
              f"{r.get('advisories', 0):>5}{r.get('suppression', 0):>6.1%}  "
              f"{', '.join(r.get('all_advised', [])) or '-'}")

    print("\n  Read this as: identical injection, different airframe response. A frame that")
    print("  did not boot or arm is reported as such -- absence of findings there means the")
    print("  aircraft never flew, not that the fault was benign.")

    Path(args.out).write_text(json.dumps(results, indent=1))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
