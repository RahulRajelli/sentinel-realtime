# Probe justification — reduced rate-loop damping on `hot_gains`

**Written and committed BEFORE the probe flights, deliberately.** `docs/METHOD.md` step 4 says the
structural property must be justified on its own merits before anyone checks whether it
discriminates. If the reasoning is written afterwards it is indistinguishable from tuning the
experiment into existence, including to the person who did it.

Date: 2026-08-15. Status at time of writing: **prediction, not result.**

---

## What is already ruled out, and why

`hot_gains` (ambiguous pair C) has a root cause, `control_oscillation`, whose detector cannot fire.
It needs, per 1.5 s window: `max|desired-actual| >= 3.0 deg` **and** `>= 3.5 zero-crossings/s`,
over `>= 2` consecutive windows. Eight probe flights are recorded in
`scripts/r7_r8_scenarios.py`:

| gain set | best amplitude | zc/s | qualifying windows |
|---|---|---|---|
| ang15 rat0.45 still | 0.96 | 0.00 | 0 |
| ang30 rat0.90 still | 1.24 | 4.10 | 0 |
| ang45 rat1.30 still | 1.39 | 23.97 | 0 |
| ang30 rat0.90 wind14 | 1.94 | 18.18 | 0 |
| **ang30 rat0.90 wind20** | **2.44** | 13.34 | 0 |
| ang30 rat0.90 wind28 | 1.81 | 16.53 | 0 |
| ang45 rat1.30 wind28 | 2.19 | 23.56 | 0 |
| ang45 rat1.30 wind36 | 1.80 | 5.39 | 0 |

The frequency criterion is met trivially. The amplitude criterion never is. Two structural reasons
are already recorded:

1. Raising proportional gain makes the controller track **more tightly**, so tracking error shrinks
   while ringing frequency rises. **P is the frequency lever, not the amplitude lever.**
2. In guided flight `ATTITUDE_TARGET` itself leans into the wind to hold position, so desired
   follows actual and the error stays bounded regardless of wind strength.

## The untested lever

**Every one of those eight probes varied proportional gain (`ATC_ANG_*_P`, `ATC_RAT_*_P`) and
wind. None varied the derivative term.**

That matters because P and D do different jobs. P sets loop bandwidth. **D is the damping term.**
Reducing D makes the closed loop underdamped, which increases *overshoot amplitude* in response to
a disturbance — a different mechanism from the one reason 1 rules out. Reason 1 says you cannot get
amplitude by tracking harder; it says nothing about getting amplitude by damping less.

Reason 2 still applies and is the real risk: if the setpoint follows the airframe, error stays
bounded no matter how badly damped the loop is. That is precisely what this probe tests.

**This is not a detector change.** `OSCILLATION_AMPLITUDE_DEG` stays at 3.0. The fault is made
larger; the bar is not lowered. Those are different actions and only the second is forbidden.

## Physical legitimacy

An underdamped rate loop is not a contrivance. **High P with insufficient D is the single most
common real-world multirotor tuning failure** — it is what an airframe does after someone raises
gains chasing crisp response and never re-tunes damping, and it is why ArduPilot ships an autotune
routine at all. The resulting limit-cycle oscillation is the textbook symptom. If anything this is
*more* representative of a real badly-tuned aircraft than the pure-P sweeps already tried.

## Configurations to fly

Default `ATC_RAT_RLL_D` / `ATC_RAT_PIT_D` on SITL copter is 0.0036. Holding the measured best
disturbance (wind 20) and moderate gains (ang30 / rat0.90) from the probe table:

| # | ATC_RAT_*_D | notes |
|---|---|---|
| D1 | 0.0036 | default — control, reproduces the 2.44 deg baseline |
| D2 | 0.0010 | lightly damped |
| D3 | 0.0000 | undamped rate loop |
| D4 | 0.0000 + `INS_GYRO_FILTER` 4 Hz | adds phase lag, which further destabilises |

## Prediction, stated now

If amplitude is limited by **damping**, D3/D4 exceed 3.0 deg and pair C completes.

If amplitude is limited by **setpoint-following** (reason 2), amplitude stays near 2.44 deg across
the whole sweep and no configuration qualifies.

## What falsifies this, and what happens then

**Falsifier: `max|desired-actual|` stays <= 2.44 deg across D1–D4.**

If that happens, the conclusion is that pair C is unreachable by *any* disturbance-based method in
SITL, because the setpoint tracks the airframe. That gets recorded as a fourth unreachable fault
alongside `gps_high_hdop`, `control_oscillation` and pair B — a genuine negative finding, published
with its measurements.

**It does NOT get fixed by lowering `OSCILLATION_AMPLITUDE_DEG`.** That would make the pair pass by
redefining the fault, which is the one move this project cannot make.

The remaining untried avenue would then be the one the scenario file already names: drive an
oscillating setpoint via `SET_ATTITUDE_TARGET` at ~2 Hz, modelling pilot-induced oscillation. That
is a different fault story and needs its own justification before use — not a fallback to be
reached for because this one failed.
