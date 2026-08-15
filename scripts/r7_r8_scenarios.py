#!/usr/bin/env python3
"""R7 fault-injection scenarios + R8 measured verification table.

Runs each scenario end-to-end against a FRESH SITL instance. Fresh matters: clip counters
are cumulative since boot and injected parameters persist, so reusing one simulator lets
state leak between scenarios and quietly invalidates the comparison.

Per scenario: fly, inject at a fixed offset, watch the live loop through the escalation
gate, and record what was advised versus what was injected.

R8's gates, all reported rather than asserted quietly:
  * NULL      -- inject nothing; any advisory at all is a hallucination and fails the run
  * pre-inject silence -- advisories before injection are false positives, counted per run
  * root cause -- credit only when the advised type matches the injected fault, never for
                  the symptom cascade it drags along
  * detection latency, suppression ratio, and detector cost vs buffer size
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
# flightdx lives in the sibling analyzer tree rather than being installed. This previously
# resolved only if the active environment happened to have it on the path, so the script worked
# on one machine and died at import on another. Same shim as conftest.py; an editable install
# (`pip install -e ../ardupilot-log-analyzer`) makes both redundant.
_FLIGHTDX_SRC = _ROOT.parent / "ardupilot-log-analyzer" / "src"
if _FLIGHTDX_SRC.exists():
    sys.path.insert(1, str(_FLIGHTDX_SRC))

from pymavlink import mavutil

from sentinel.bundle import InjectedParam
from sentinel.capture import BundleRecorder, compare_results, metrics_from_result
from sentinel.gate import EscalationGate
from sentinel.memory import FlightHistory
from sentinel.runner import LiveRunner

WSL_DISTRO = "Ubuntu-24.04"
AP = "/root/ardupilot"

# Injections and the incident type each is expected to *cause*. Parameter names come from
# sitl-harness/sim_params.json; SIM_VIB_MOT_MAX is a frequency and SIM_ACC1_RND the
# amplitude, per AP_InertialSensor_SITL.cpp:142-156 -- neither works without the other.
#
# "symptoms" lists the cascade a CORRECT system is permitted to also raise. It is written down
# rather than inferred because naming one of these as the root cause is the specific failure E4
# exists to measure -- a scorer that had to guess which advisories were symptoms would be
# grading against its own opinion. Types verified against flightdx/detectors/*.py; the list is
# what is *permitted*, not what was observed, so a longer run that finally degrades the EKF does
# not retroactively count as a new failure.
SCENARIOS = {
    "null": {
        "inject": [],
        "expect": None,
        "symptoms": [],
        "note": "hallucination gate: nothing injected, nothing may be advised",
    },
    "vibration": {
        "inject": [("SIM_VIB_MOT_MAX", 200.0), ("SIM_ACC1_RND", 40.0)],
        "expect": "vibration_excessive",
        "symptoms": ["accel_clipping"],
        "note": "amplitude x frequency; accel_clipping is an expected downstream symptom",
    },
    "gps_loss": {
        "inject": [("SIM_GPS1_FIXTYPE", 0.0)],
        "expect": "gps_fix_loss",
        "symptoms": ["ekf_inconsistency", "gps_high_hdop"],
        "note": "fix type 0; EKF degradation is the expected cascade, not the root cause",
    },
    "wind": {
        "inject": [("SIM_WIND_SPD", 18.0), ("SIM_WIND_DIR", 90.0)],
        "expect": None,
        "symptoms": ["actuator_saturation", "control_oscillation"],
        "note": "confounder: mimics actuator asymmetry; a clean system stays quiet",
    },

    # ---- ambiguous pairs -------------------------------------------------------------
    #
    # The four scenarios above share a property that makes them useless for measuring an
    # agent: in every one of them the root cause's own detector is the first to fire, so
    # "first advisory" and "root cause" never come apart and the deterministic tier scores
    # 4/4 at zero cost. These two are built to break that, and each declares
    # `first_advisory` -- the SYMPTOM it must advise first. If the symptom does not actually
    # arrive first the scenario is not ambiguous, and the run reports that rather than
    # quietly grading an experiment that tested nothing.
    #
    # Both keep the true answer inside the detector vocabulary, deliberately. A root cause
    # no detector can name would make the baseline wrong by construction, and a rigged
    # comparison is worth no more against the baseline than for it.
    #
    # Cadence is tightened to 0.25 s here. At 1 Hz two events 300 ms apart land in the same
    # cycle, and the gate then breaks the tie by DETECTORS list order (runner.py:39) --
    # which is arbitrary, and must not be the thing the experiment measures.

    "compass_offset": {
        "inject": [("SIM_MAG1_OFS_X", 400.0), ("SIM_MAG1_OFS_Y", 400.0)],
        "expect": "compass_inconsistency",
        "symptoms": ["ekf_inconsistency"],
        "first_advisory": "ekf_inconsistency",
        "cadence_s": 0.25,
        "duration_s": 45.0,
        "note": ("ambiguous pair A: a magnetometer offset raises EKF mag variance at once, but "
                 "compass.py:45 MIN_ANOMALY_S=1.0 requires the anomaly to persist a full second "
                 "before the compass detector will call it. ekf.py has no such gate "
                 "(WARNING_THRESHOLD=1.0 fires on crossing). The symptom therefore leads the "
                 "cause by construction, not by tuning"),
    },
    # Pair C. Replaces pair B, which is RETIRED -- see the note on `stiff_airframe` below.
    #
    # The strongest structural guarantee in the detector set. `oscillation.py` needs
    # WINDOW_SIZE_S=1.5 of ATT history AND MIN_SUSTAINED_WINDOWS=2 consecutive windows, so
    # `control_oscillation` CANNOT be raised for ~3 s after onset -- three times pair A's 1.0 s
    # and, like pair A, a consequence of constants in the source rather than of tuning. Any
    # detector that fires on threshold crossing therefore leads it automatically.
    #
    # This was chosen over the build plan's own fallback candidate (`gps_high_hdop` as root with
    # `ekf_inconsistency` leading), which cannot be built at all:
    #   * `gps.py` has NO persistence gate -- its HDOP check fires on the first sample over 2.0,
    #     so ordering would be decided by physics and, on a tie, by DETECTORS registration order.
    #     That is the artifact 10.3 exists to avoid, not a mechanism.
    #   * SITL cannot raise HDOP anyway. `SIM_GPS_UBLOX.cpp:284` hardcodes `dop.hDOP = 121`
    #     (1.21), below the 2.0 threshold, and no SIM_GPS1_* parameter touches it. Confirmed in
    #     our own data: `gps_high_hdop` is a declared symptom of `gps_loss` and has never fired.
    #
    # `first_advisory` is a LIST here, unlike pair A's single string. The structural claim is
    # "the root cause is not advised first" -- naming one specific symptom would over-specify it
    # and could fail a confounder that worked perfectly, just via the other symptom.
    #
    # HONEST LIMIT: the gain values below still need one probe flight, because the oscillation
    # has to be big enough (>=3 deg) and fast enough (>=1.75 Hz) for the detector to see it at a
    # 10 Hz ATT stream. That tuning decides whether the fault is DETECTED; it does not decide the
    # ORDERING, which is what separates this from pair B.
    # STATUS 2026-08-14: BLOCKED, and the block is in SITL's physics, not in this config.
    #
    # `control_oscillation` needs, per 1.5 s window: max|desired-actual| >= 3.0 deg AND >= 3.5
    # zero-crossings/s, over >= 2 consecutive windows. Six probe flights measured the actual
    # tracking-error signal against exactly those two numbers:
    #
    #   gain set                 best amp    zc/s     qualifying windows
    #   ang15 rat0.45 still        0.96      0.00        0
    #   ang30 rat0.90 still        1.24      4.10        0
    #   ang45 rat1.30 still        1.39     23.97        0
    #   ang30 rat0.90 wind14       1.94     18.18        0
    #   ang30 rat0.90 wind20       2.44     13.34        0     <- best
    #   ang30 rat0.90 wind28       1.81     16.53        0
    #   ang45 rat1.30 wind28       2.19     23.56        0
    #   ang45 rat1.30 wind36       1.80      5.39        0
    #
    # The FREQUENCY criterion is trivially met. The AMPLITUDE criterion never is, and it does not
    # respond to the obvious levers: 10x the default angle P moved it 0.96 -> 1.39 deg, and wind
    # is non-monotonic past ~20 m/s. Two reasons, both structural:
    #   * raising gains makes the controller track MORE tightly, so tracking error shrinks while
    #     the ringing frequency rises -- gains are the frequency lever, not the amplitude one;
    #   * in guided flight ATTITUDE_TARGET itself leans into the wind to hold position, so
    #     desired follows actual and the ERROR stays bounded however hard the air pushes.
    # 3.0 deg of sustained tracking error is a real airframe with mechanical slop, not clean
    # SITL physics.
    #
    # NOT fixed by lowering OSCILLATION_AMPLITUDE_DEG. Tuning the detector to fit the experiment
    # would make the pair pass by redefining the fault, which is the one move this project cannot
    # make.
    #
    # The untried avenue, if this is picked up again: drive an oscillating SETPOINT via
    # SET_ATTITUDE_TARGET at ~2 Hz instead of waiting for the airframe to self-oscillate. That
    # produces genuine tracking error at a chosen frequency and models pilot-induced oscillation.
    # It is a different fault story, so it needs its own justification before use.
    #
    # Gain choice, measured rather than guessed. Probe 1 used rate-loop P alone
    # (ATC_RAT_RLL_P/PIT_P = 0.45): actuator_saturation fired at +1.8 s and control_oscillation
    # NEVER fired. Correct ordering, undetected root cause -- the rate loop buzzes fast and
    # narrow, so the airframe never swings the >=3 deg of ATTITUDE that oscillation.py needs.
    # The angle loop is the one that oscillates slowly and widely, so it drives the fault; the
    # rate gain stays because it is what produced the saturation symptom.
    "hot_gains": {
        # Probe 2 (ANG_P=15 + RAT_P=0.45, still air): actuator_saturation escalated to critical,
        # control_oscillation still absent. Confirmed by probe 3 that the live path DOES supply a
        # desired attitude (SITL sends ATTITUDE_TARGET and NAV_CONTROLLER_OUTPUT at 10 Hz, and
        # ATT clears MIN_ATT_RATE_HZ=7.0), so the detector can run -- it simply had no tracking
        # error to see. detect_oscillation measures desired-minus-actual, and a guided hover
        # holding a level setpoint in still air never rings.
        #
        # Wind is the excitation, not the fault. The `wind` scenario alone is a CONFOUNDER that a
        # correct system stays quiet through (expect=None, and it does: 0 advisories in all three
        # re-flown reps). Oscillation here therefore comes from the gains, which is what
        # `control_oscillation` names -- and it matches how the fault presents in the field,
        # where a mis-tuned airframe flies fine until it meets air that pushes back.
        "inject": [("ATC_ANG_RLL_P", 22.0), ("ATC_ANG_PIT_P", 22.0),
                   ("ATC_RAT_RLL_P", 0.70), ("ATC_RAT_PIT_P", 0.70),
                   ("SIM_WIND_SPD", 14.0)],
        "expect": "control_oscillation",
        "symptoms": ["actuator_saturation", "ekf_inconsistency"],
        "first_advisory": ["actuator_saturation", "ekf_inconsistency"],
        "cadence_s": 0.25,
        "duration_s": 45.0,
        "note": ("ambiguous pair C: oscillation.py needs WINDOW_SIZE_S=1.5 x "
                 "MIN_SUSTAINED_WINDOWS=2, so control_oscillation cannot be advised for ~3 s "
                 "after onset, while actuator_saturation and ekf_inconsistency both fire on "
                 "threshold crossing with no time gate. The symptom leads the cause by a "
                 "window length, not by tuning"),
    },
    # Pair C, UNBLOCKED -- 2026-08-15. Added as a separate scenario rather than by editing
    # `hot_gains` above, because the eight probe measurements recorded in that entry's comments
    # describe ITS parameter set. Mutating it would silently invalidate every one of them.
    #
    # Those eight probes swept proportional gain and wind and peaked at 2.44 deg against a 3.0 deg
    # threshold, and recorded two structural reasons it could not be beaten: raising P makes the
    # controller track more tightly (P is the frequency lever), and in guided flight the attitude
    # target leans with the airframe so error stays bounded.
    #
    # None of them varied DAMPING. A four-point sweep (docs/probe-hot-gains-damping.md, written
    # and committed BEFORE flying, with its falsifier stated) measured:
    #
    #   ATC_RAT_*_D   INS_GYRO_FILTER   best amp   verdict
    #   0.0036 (dflt) default            2.36 deg   no
    #   0.0010        default            2.33 deg   no
    #   0.0000        default            2.62 deg   no
    #   0.0000        4 Hz              53.51 deg   FIRES
    #
    # Damping alone was worth +11% and was not enough. The decisive lever was the phase lag from
    # over-filtering the gyro -- a common real mistuning, since people filter hard to hide
    # vibration and destabilise the loop doing it.
    #
    # VERIFIED NOT A CRASH. 53 deg of tracking error is departure territory, so it was checked
    # rather than assumed: altitude held (7.20 m start, 7.20 m minimum, 8.71 m end), the vehicle
    # stayed armed for the full 30 s, and the error alternates sign continuously instead of
    # diverging. It is a violent but genuine limit cycle.
    #
    # OSCILLATION_AMPLITUDE_DEG is untouched at 3.0. The fault was made larger; the bar was not
    # lowered. Those are different actions and only the second is forbidden.
    #
    # HONEST LIMIT: this is a severe fault, not a subtle one. A +-40 deg limit cycle would alarm
    # any operator, where pair A's compass offset is invisible until the advisory. Whether the
    # ORDERING holds is what this scenario exists to measure.
    #
    # FLOWN 2026-08-15 -- bundles/hot_gains_lowd_pairc_r0.json. The ordering holds:
    #
    #   t =  8.000   inject
    #   t = 11.062   actuator_saturation   SYMPTOM   critical   (+3.062)
    #   t = 12.781   control_oscillation   CAUSE     critical   (+4.781)
    #                gap 1.719 s, 0 pre-injection false positives
    #                repeats within the same flight at +23.062 / +24.828, gap 1.766 s
    #
    # THE GAP IS 1.72 s, NOT THE ~3 s THIS COMMENT ORIGINALLY PREDICTED. The 3 s gate is real --
    # 2 x 1.5 s windows after the amplitude first crosses 3.0 deg -- but it was wrong to compare
    # it against pair A's 1.0 s, because that 1.0 s is a measured GAP and this 3 s is a detector
    # LATENCY. They are not the same quantity. The symptom is not instantaneous either:
    # actuator_saturation needs the oscillation to drive the outputs to their limits, which took
    # 3.062 s, so most of the gate elapses before either detector speaks. Ordering is guaranteed
    # by construction; the visible margin is what the flight measures, and it is 1.72 s -- larger
    # than pair A's, but for a different reason than the one predicted here.
    "hot_gains_lowd": {
        "inject": [("ATC_ANG_RLL_P", 30.0), ("ATC_ANG_PIT_P", 30.0),
                   ("ATC_RAT_RLL_P", 0.90), ("ATC_RAT_PIT_P", 0.90),
                   ("ATC_RAT_RLL_D", 0.0), ("ATC_RAT_PIT_D", 0.0),
                   ("INS_GYRO_FILTER", 4.0),
                   ("SIM_WIND_SPD", 20.0)],
        "expect": "control_oscillation",
        "symptoms": ["actuator_saturation", "ekf_inconsistency"],
        "first_advisory": ["actuator_saturation", "ekf_inconsistency"],
        "cadence_s": 0.25,
        "duration_s": 45.0,
        "note": ("pair C unblocked by removing rate-loop damping and over-filtering the gyro. "
                 "oscillation.py still needs WINDOW_SIZE_S=1.5 x MIN_SUSTAINED_WINDOWS=2, so "
                 "control_oscillation cannot be advised until 3.0 s after the amplitude first "
                 "crosses, while actuator_saturation and ekf_inconsistency fire on threshold "
                 "crossing with no gate. MEASURED GAP 1.719 s (repeat 1.766 s in the same "
                 "flight), not the ~3 s that gate suggests: the symptom is itself delayed "
                 "3.062 s while the oscillation builds, so most of the gate elapses before "
                 "either detector speaks. Amplitude measured at 53.5 deg, verified as a "
                 "sustained limit cycle rather than a departure (altitude held, stayed armed)"),
    },

    # RETIRED 2026-08-14, kept as the record of what was tried. Flown 3x at SIM_ACC1_RND=90 and
    # 3x at 70: `ambiguity_confirmed` false in all 6, with vibration_excessive and accel_clipping
    # landing in the SAME 0.25 s cycle every time, so the gate broke the tie on DETECTORS order.
    #
    # It cannot be fixed by sweeping the amplitude, and 110 would not have helped either.
    # accel_clipping needs instantaneous peaks past the ~16 g sensor range; vibration_excessive
    # needs filtered VIBE >= 30 m/s^2, an RMS-like measure. SIM_ACC1_RND is a Gaussian noise
    # amplitude, so it moves peak and RMS TOGETHER by the same sigma: any value with frequent
    # clipping also has VIBE far above threshold, and any value low enough to delay VIBE stops
    # clipping altogether. One parameter cannot separate the two.
    "stiff_airframe": {
        "inject": [("SIM_ACC1_RND", 90.0), ("SIM_VIB_MOT_MAX", 120.0)],
        "expect": "vibration_excessive",
        "symptoms": ["accel_clipping"],
        "first_advisory": "accel_clipping",
        "cadence_s": 0.25,
        "duration_s": 45.0,
        "note": ("ambiguous pair B: CLIP_WARNING_COUNT=1 fires on a single new clip, while "
                 "vibration_excessive needs filtered VIBE to reach 30 m/s^2, which builds over "
                 "seconds. NEEDS A TUNING PASS -- amplitude must clip immediately yet keep VIBE "
                 "under threshold for at least one cycle. See L1-E4-BUILD-PLAN.md 10.3"),
    },
}


def _leading(cfg: dict) -> tuple[str, ...]:
    """The symptom(s) that may legitimately be advised before the root cause.

    Accepts a single string (pair A pins one exact symptom) or a list (pair C, where either
    fast-firing detector satisfies the structural claim). The claim an ambiguous pair makes is
    "the root cause is NOT first"; pinning one symptom when two qualify would fail a confounder
    that worked, via the wrong-but-still-correct symptom.
    """
    first = cfg["first_advisory"]
    return (first,) if isinstance(first, str) else tuple(first)


def port_for_instance(instance: int) -> int:
    """ArduPilot SITL binds 5760 + 10*instance, and IGNORES any port given in --serial0.

    Measured 2026-07-30: `-I2 --serial0 tcp:0.0.0.0:5800` bound 5780, not 5800, so the
    harness sat in a connect-retry loop against a port nothing was listening on. The
    instance number is the only thing that decides the port.
    """
    return 5760 + 10 * instance


def launch_sitl(instance: int) -> subprocess.Popen:
    # No --serial0 override: it would be ignored for the port and only mislead a reader.
    cmd = (
        f"mkdir -p /root/sitl_s{instance} && cd /root/sitl_s{instance} && "
        f"rm -f eeprom.bin && "
        f"{AP}/build/sitl/bin/arducopter -S -I{instance} --model quad --speedup 1 "
        f"--defaults {AP}/Tools/autotest/default_params/copter.parm "
        f"--home 12.9716,77.5946,900,0"
    )
    return subprocess.Popen(
        ["wsl", "-d", WSL_DISTRO, "--", "bash", "-c", cmd],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def set_param(conn, name, value):
    conn.mav.param_set_send(
        conn.target_system, conn.target_component,
        name.encode("utf-8"), float(value), mavutil.mavlink.MAV_PARAM_TYPE_REAL32)


def read_param(conn, name, timeout=4.0):
    """Read one parameter, ignoring anything already queued for it.

    The drain is load-bearing, and was added after it produced a false failure. `SIM_MAG1_OFS`
    is an AP_Vector3f (SITL.h:204): writing SIM_MAG1_OFS_X makes ArduPilot broadcast PARAM_VALUE
    for all three components, so a stale `SIM_MAG1_OFS_Y = 13` (its default, SITL.h:156) sits in
    the buffer. Setting Y next and reading it back then matched that stale message and returned
    13 -- reporting the injection as not applied when it had applied fine. Measured 2026-08-14:
    undrained read 13.0, drained read 400.0, same connection, same instant.

    Scenarios injecting unrelated params never saw this, which is why it survived 12 flights:
    only a vector param broadcasts siblings the reader is about to ask for.
    """
    while conn.recv_match(type="PARAM_VALUE", blocking=False) is not None:
        pass
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


def arm_and_takeoff(conn, alt=10.0):
    """Get airborne on a freshly booted SITL.

    10 m rather than 20: the climb runs at speedup 1 (kept deliberately so detection
    latency is wall-clock and honest), and altitude is irrelevant to these faults.

    A GPS fix alone is NOT enough to arm. SITL reports fix_type 6 within a couple of
    seconds while the EKF is still converging, so arming is rejected on pre-arm checks.
    A first version of this harness waited only for the fix and every scenario failed with
    "could not arm"; the R0 script that armed reliably had waited for EKF health too.
    Pre-arm rejections also arrive as STATUSTEXT, so those are captured and reported
    instead of returning a bare failure.
    """
    # EKF_STATUS_FLAGS bits. Checking `flags != 0` is useless: the attitude bit sets almost
    # immediately, and the variances start near zero precisely BECAUSE nothing is being
    # estimated yet. Arming needs an absolute horizontal position, which is these bits.
    EKF_POS_HORIZ_ABS = 16
    EKF_CONST_POS_MODE = 128
    EKF_PRED_POS_HORIZ_ABS = 512

    fix, ekf_ok, have_home = 0, False, False
    end = time.time() + 240
    while time.time() < end and not (fix >= 3 and ekf_ok and have_home):
        m = conn.recv_match(blocking=True, timeout=5)
        if m is None:
            continue
        t = m.get_type()
        if t == "GPS_RAW_INT":
            fix = m.fix_type
        elif t == "EKF_STATUS_REPORT":
            f = m.flags
            ekf_ok = (bool(f & EKF_POS_HORIZ_ABS)
                      and bool(f & EKF_PRED_POS_HORIZ_ABS)
                      and not (f & EKF_CONST_POS_MODE))
        elif t in ("HOME_POSITION", "GPS_GLOBAL_ORIGIN"):
            # "Arm: AHRS: waiting for home" is a distinct pre-arm gate from the EKF one.
            have_home = True

    if fix < 3:
        return False, "no 3D fix"
    if not ekf_ok:
        return False, "EKF never reached absolute position (POS_HORIZ_ABS)"
    if not have_home:
        return False, "home position never set"

    conn.set_mode("GUIDED")
    time.sleep(1)

    reasons: list[str] = []
    armed = False
    for _ in range(15):
        conn.mav.command_long_send(
            conn.target_system, conn.target_component,
            mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0)
        deadline = time.time() + 4
        while time.time() < deadline:
            m = conn.recv_match(blocking=True, timeout=1)
            if m is None:
                continue
            t = m.get_type()
            if t == "STATUSTEXT":
                txt = m.text.decode() if isinstance(m.text, bytes) else str(m.text)
                if "arm" in txt.lower() or "prearm" in txt.lower():
                    if txt not in reasons:
                        reasons.append(txt)
            elif (t == "COMMAND_ACK"
                  and m.command == mavutil.mavlink.MAV_CMD_COMPONENT_ARM_DISARM):
                if m.result == 0:
                    armed = True
                break
        if armed:
            break
        time.sleep(1.5)

    if not armed:
        return False, f"could not arm — {'; '.join(reasons[-3:]) or 'no reason reported'}"

    conn.mav.command_long_send(
        conn.target_system, conn.target_component,
        mavutil.mavlink.MAV_CMD_NAV_TAKEOFF, 0, 0, 0, 0, 0, 0, 0, alt)
    conn.recv_match(type="COMMAND_ACK", blocking=True, timeout=6)

    peak, end = 0.0, time.time() + 120
    while time.time() < end and peak < alt * 0.7:
        m = conn.recv_match(type="GLOBAL_POSITION_INT", blocking=True, timeout=5)
        if m:
            peak = max(peak, m.relative_alt / 1000.0)
    return (peak >= alt * 0.5), f"peak {peak:.1f} m"


def run_scenario(name: str, cfg: dict, instance: int,
                 inject_at: float, duration: float,
                 bundles_dir: Path | None = None, rep: int = 0,
                 tag: str | None = None, airframe_id: str = "sitl-quad") -> dict:
    port = port_for_instance(instance)
    print(f"\n{'=' * 78}\nSCENARIO: {name}   ({cfg['note']})")
    print(f"  SITL instance {instance} -> tcp:127.0.0.1:{port}\n{'=' * 78}")
    proc = launch_sitl(instance)
    result = {"scenario": name, "expected": cfg["expect"], "ok": False}

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
            result["error"] = "no heartbeat"
            return result

        # Ambiguous scenarios tighten the cadence so two events a few hundred ms apart land in
        # different cycles. At 1 Hz they collide and the gate breaks the tie by detector
        # registration order, which would make the experiment measure a list.
        cadence = float(cfg.get("cadence_s", 1.0))
        duration = float(cfg.get("duration_s", duration))
        runner = LiveRunner(conn, cadence_s=cadence, window_s=120.0)
        runner.request_streams(rate_hz=10.0)
        runner.fetch_params()

        ok, why = arm_and_takeoff(conn)
        print(f"  takeoff: {why}")
        if not ok:
            result["error"] = why
            return result

        gate = EscalationGate(cooldown_s=20.0, clear_after_s=8.0)
        state = {"injected_at": None, "verified": False,
                 "first": None, "first_t": None, "pre_inject": []}
        advisories = []

        # Pure observer. It records what the loop below already computes and never feeds back
        # into it -- the refactor that added capture must not be able to move an R8 number.
        recorder = BundleRecorder(
            scenario=name, note=cfg["note"],
            expected_root_cause=cfg["expect"],
            expected_symptoms=cfg.get("symptoms", []),
        )

        def on_cycle(r):
            if state["injected_at"] is None and r.t >= inject_at:
                good = True
                applied: list[InjectedParam] = []
                for pname, pval in cfg["inject"]:
                    set_param(conn, pname, pval)
                    time.sleep(0.3)
                    back = read_param(conn, pname)
                    ok_one = back is not None and abs(back - pval) < max(0.01, abs(pval) * 0.01)
                    good &= ok_one
                    applied.append(InjectedParam(name=pname, value=pval,
                                                 readback=back, applied=bool(ok_one)))
                    print(f"    inject {pname}={pval} -> readback {back}")
                state["verified"] = good or not cfg["inject"]
                state["injected_at"] = r.t
                recorder.on_injection(applied, r.t, state["verified"])
                if not cfg["inject"]:
                    print("    (null scenario: nothing injected)")

            raised = gate.submit(r.incidents, r.t)
            for adv in raised:
                entry = {"t": round(r.t, 1), "type": adv.incident.type,
                         "severity": adv.incident.severity, "reason": adv.reason}
                advisories.append(entry)
                pre = state["injected_at"] is None
                recorder.on_advisory(r.t, adv.incident, adv.reason, pre_inject=pre)
                if pre:
                    state["pre_inject"].append(entry)
                    print(f"  {r.t:6.1f}s  FALSE POSITIVE (pre-injection): "
                          f"{adv.incident.type}[{adv.incident.severity}]")
                else:
                    if state["first"] is None:
                        state["first"] = adv.incident.type
                        state["first_t"] = r.t
                    print(f"  {r.t:6.1f}s  {adv.incident.type}[{adv.incident.severity}]"
                          f" ({adv.reason})")

            # Last, so a recorder bug cannot change what the harness above decided.
            recorder.on_cycle(r)

        reports = runner.run(duration, on_cycle=on_cycle)

        latency = (state["first_t"] - state["injected_at"]
                   if state["first_t"] is not None and state["injected_at"] is not None else None)
        raised_types = sorted({a["type"] for a in advisories
                               if a not in state["pre_inject"]})

        if cfg["expect"] is None:
            ok = len(advisories) == 0
        elif "first_advisory" in cfg:
            # An ambiguous scenario is CORRECT when the symptom leads and the cause follows.
            # Scoring it by "did the gate name the root cause" would mark a working confounder
            # as a failure -- the gate is supposed to be wrong here. That is the point.
            ok = (state["first"] in _leading(cfg)
                  and cfg["expect"] in raised_types
                  and not state["pre_inject"])
        else:
            ok = state["first"] == cfg["expect"] and not state["pre_inject"]

        result.update({
            "ok": ok,
            "injected": [f"{p}={v}" for p, v in cfg["inject"]],
            "inject_verified": state["verified"],
            "first_advised": state["first"],
            "latency_s": round(latency, 1) if latency is not None else None,
            "all_advised": raised_types,
            "false_positives": len(state["pre_inject"]),
            "incidents": gate.stats.seen,
            "advisories": gate.stats.raised,
            "suppression": round(gate.stats.suppression_ratio, 3),
            "cycles": len(reports),
            "buffer_first": reports[0].buffer_records if reports else 0,
            "buffer_last": reports[-1].buffer_records if reports else 0,
            "detect_ms_first": round(reports[0].detect_ms, 2) if reports else 0,
            "detect_ms_last": round(reports[-1].detect_ms, 2) if reports else 0,
            "worst_cycle_ms": round(max((r.total_ms for r in reports), default=0), 1),
        })

        if "first_advisory" in cfg:
            # Reported separately from `ok` so a pair that stopped being ambiguous is visible
            # rather than merely failing. If the cause now leads the symptom, the scenario has
            # to be retuned -- grading judges against it would measure nothing.
            result["ambiguous"] = True
            result["expected_first"] = cfg["first_advisory"]
            result["ambiguity_confirmed"] = (state["first"] in _leading(cfg)
                                             and cfg["expect"] in raised_types)
            result["cadence_s"] = cadence

        if bundles_dir is not None:
            bundle = recorder.finish(params=runner.params,
                                     metrics=metrics_from_result(result))
            # Airframe identity, so the flight can join a cross-flight history. Set BEFORE save
            # so the file on disk carries it; excluded from bundle_id by design (bundle.py), so
            # setting it does not change the fingerprint of the flight.
            bundle.airframe_id = airframe_id
            stem = f"{name}_{tag}_r{rep}" if tag else f"{name}_r{rep}"
            path = bundle.save(Path(bundles_dir) / f"{stem}.json")
            # Durable memory. Append-only, and appended AFTER the bundle is safely written so a
            # crash between the two leaves history short rather than claiming a flight that has
            # no file behind it.
            FlightHistory().record_flight(bundle, airframe_id)
            result["bundle"] = str(path)
            result["bundle_id"] = bundle.bundle_id
            print(f"  bundle: {path.name}  id={bundle.bundle_id}  "
                  f"cycles={len(bundle.cycles)} advisories={len(bundle.advisories)}")

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", default="null,vibration,gps_loss,wind")
    ap.add_argument("--inject-at", type=float, default=8.0)
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--out", default="r8_results.json")
    ap.add_argument("--bundles", default=None,
                    help="directory to write RunBundles into; omit to skip capture entirely")
    ap.add_argument("--repeat", type=int, default=1,
                    help="flights per scenario. n=4 makes every confidence interval "
                         "embarrassing; 3 repeats gives n=12 for ~48 min of SITL and no code")
    ap.add_argument("--tune", action="append", default=[], metavar="SCEN:PARAM=VALUE",
                    help="override one injection value for a tuning sweep (repeatable), e.g. "
                         "--tune stiff_airframe:SIM_ACC1_RND=70. Pair B's procedure is a sweep "
                         "(L1-E4-BUILD-PLAN.md 10.3), and hand-editing the SCENARIOS dict "
                         "between runs is how a sweep loses track of which value produced "
                         "which bundle. Implies --tag unless one is given")
    ap.add_argument("--tag", default=None,
                    help="suffix for bundle filenames, e.g. 'a70' -> stiff_airframe_a70_r0.json. "
                         "Without it a re-tuned flight silently overwrites the bundle it is "
                         "meant to be compared against")
    ap.add_argument("--compare", default=None,
                    help="baseline r8_results.json to diff against. Exact on the claim-bearing "
                         "fields, toleranced on the wall-clock ones -- see capture.py")
    args = ap.parse_args()

    names = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    bundles_dir = Path(args.bundles) if args.bundles else None

    # Tuning overrides. Applied to a COPY: SCENARIOS is module state and a mutated entry would
    # leak into anything that imports this module (the tests do), making the fault library depend
    # on whether a sweep had been run first.
    scenarios = {n: dict(SCENARIOS[n]) for n in names}
    tag = args.tag
    for spec in args.tune:
        try:
            scen, assignment = spec.split(":", 1)
            pname, pval = assignment.split("=", 1)
            pval = float(pval)
        except ValueError:
            print(f"bad --tune {spec!r}; want SCEN:PARAM=VALUE", file=sys.stderr)
            return 1
        if scen not in scenarios:
            print(f"--tune names {scen!r}, not in --scenarios {names}", file=sys.stderr)
            return 1
        inject = [list(p) for p in scenarios[scen]["inject"]]
        hits = [p for p in inject if p[0] == pname]
        if not hits:
            # Adding a parameter the scenario never injected is almost always a typo, and
            # silently accepting it would produce a bundle labelled as a tuning point of a
            # scenario it does not belong to.
            print(f"--tune {scen}:{pname} -- that scenario injects "
                  f"{[p[0] for p in inject]}", file=sys.stderr)
            return 1
        for p in hits:
            print(f"tune: {scen}.{pname} {p[1]} -> {pval}")
            p[1] = pval
        scenarios[scen]["inject"] = [tuple(p) for p in inject]
        if tag is None:
            tag = f"{pname.split('_')[-1].lower()}{pval:g}"
    if tag:
        print(f"bundle tag: {tag}  (bundles land as SCENARIO_{tag}_rN.json)")

    # Load the baseline BEFORE flying. --out defaults to r8_results.json, which is also the
    # obvious thing to pass to --compare, and results are written before the comparison runs --
    # so reading it late would diff the new run against itself and always pass.
    baseline = None
    if args.compare:
        baseline = json.loads(Path(args.compare).read_text())
        if Path(args.compare).resolve() == Path(args.out).resolve():
            print(f"note: --out would overwrite --compare ({args.out}); "
                  f"baseline already read into memory, comparison is still honest")
    results = []
    # Instance 0 is reserved: a long-running SITL holds 5760 (and 5762 for the MAVProxy GUI).
    # Instances are reused across repeats, which is safe because each run terminates and pkills
    # its simulator in the `finally` before the next starts.
    for rep in range(args.repeat):
        for i, name in enumerate(names):
            r = run_scenario(name, scenarios[name], 2 + i,
                             args.inject_at, args.duration, bundles_dir, rep, tag)
            r["rep"] = rep
            results.append(r)

    print(f"\n\n{'=' * 100}")
    print("R8 — MEASURED VERIFICATION TABLE")
    print("=" * 100)
    hdr = (f"{'scenario':<11} {'injected':<34} {'advised':<22} "
           f"{'lat':>5} {'FP':>3} {'inc':>5} {'adv':>4} {'supp':>6}  ok")
    print(hdr)
    print("-" * 100)
    def lbl(r: dict) -> str:
        return r["scenario"] if args.repeat == 1 else f"{r['scenario']}#{r.get('rep', 0)}"

    for r in results:
        inj = ", ".join(r.get("injected", [])) or "(nothing)"
        adv = r.get("first_advised") or "-"
        lat = f"{r['latency_s']}s" if r.get("latency_s") is not None else "-"
        print(f"{lbl(r):<11} {inj[:34]:<34} {adv[:22]:<22} "
              f"{lat:>5} {r.get('false_positives', 0):>3} {r.get('incidents', 0):>5} "
              f"{r.get('advisories', 0):>4} {r.get('suppression', 0):>6.1%}  "
              f"{'PASS' if r['ok'] else 'FAIL'}")

    print("\n--- cascade detail (root cause vs symptoms advised) ---")
    for r in results:
        if r.get("all_advised"):
            exp = r.get("expected")
            permitted = set(SCENARIOS.get(r["scenario"], {}).get("symptoms", []))
            extras = [t for t in r["all_advised"] if t != exp]
            # An advisory that is neither the root cause nor a declared symptom is the
            # interesting case: either the fault library's symptom list is wrong, or the
            # detectors found something real that nobody wrote down.
            undeclared = [t for t in extras if t not in permitted]
            print(f"  {lbl(r):<11} root={exp or '(none expected)':<24} "
                  f"also advised: {extras or 'none'}"
                  f"{'   UNDECLARED: ' + str(undeclared) if undeclared else ''}")

    print("\n--- detector cost vs buffer size (E1 crossover input) ---")
    for r in results:
        if r.get("cycles"):
            print(f"  {lbl(r):<11} {r['buffer_first']:>5} -> {r['buffer_last']:>5} recs   "
                  f"{r['detect_ms_first']:>6.2f} -> {r['detect_ms_last']:>6.2f} ms   "
                  f"worst cycle {r['worst_cycle_ms']:>6.1f} ms of 1000 ms "
                  f"({r['worst_cycle_ms'] / 10:.1f}%)")
    print("\n  note: detect_oscillation cannot fire before ~1.5 s of ATT history (its window),")
    print("        so that is a floor on oscillation latency independent of the network.")

    Path(args.out).write_text(json.dumps(results, indent=1))
    print(f"\nwrote {args.out}")
    if bundles_dir is not None:
        print(f"wrote {sum(1 for r in results if r.get('bundle'))} bundles to {bundles_dir}/")

    regressed = False
    if baseline is not None:
        # Compare rep 0 only: the baseline is a single-flight-per-scenario run, so comparing
        # every repeat against it would diff repeats against each other by proxy.
        current = [r for r in results if r.get("rep", 0) == 0]
        failures = compare_results(baseline, current)
        print(f"\n--- refactor gate vs {args.compare} ---")
        if failures:
            regressed = True
            for f in failures:
                print(f"  REGRESSION: {f}")
            print("\n  The refactor changed behaviour. The refactor is wrong, not the baseline.")
        else:
            print("  PASS — claim-bearing fields unchanged, wall-clock fields within tolerance")

    passed = sum(1 for r in results if r["ok"])
    print(f"\nR7+R8: {passed}/{len(results)} runs passed")
    return 0 if (passed == len(results) and not regressed) else 1


if __name__ == "__main__":
    raise SystemExit(main())
