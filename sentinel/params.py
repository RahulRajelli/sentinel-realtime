"""Live parameter cache (Phase R1, live half).

The detectors need the vehicle's *configured* thresholds, not hardcoded ones -- that was
the D5 finding, where a hardcoded 1050 us floor reported motors idling at their configured
MOT_PWM_MIN of 1000 as critically saturated.

A full PARAM_REQUEST_LIST is not usable here: measured against SITL it returned 1387
parameters in 151 seconds. Startup cannot wait that long, so this requests only the ~23
parameters the detectors actually read, by name.

Until the fetch completes, callers get an empty dict. That is deliberate -- per
SENTINEL-REALTIME-MVP-SPEC.md the live tier must refuse to emit battery or actuator
verdicts rather than fall back to defaults, because falling back to defaults *is* the D5
bug reintroduced at runtime.
"""

import time

# Everything the repaired detectors read from parameters.
ACTUATOR_PARAMS = ["MOT_PWM_MIN", "MOT_PWM_MAX", "MOT_SPIN_MIN", "MOT_SPIN_MAX"]
BATTERY_PARAMS = ["BATT_LOW_VOLT", "BATT_CRT_VOLT", "BATT_ARM_VOLT"]
# RCOU.Cn maps to SERVOn; the function tells us which outputs are motors (33..44).
SERVO_FUNCTION_PARAMS = [f"SERVO{n}_FUNCTION" for n in range(1, 17)]

REQUIRED = ACTUATOR_PARAMS + BATTERY_PARAMS + SERVO_FUNCTION_PARAMS


class ParamCache:
    """Targeted parameter fetch over MAVLink."""

    def __init__(self, conn, names: list[str] | None = None) -> None:
        self.conn = conn
        self.names = list(names if names is not None else REQUIRED)
        self.values: dict[str, float] = {}
        self.complete = False

    def fetch(self, timeout: float = 20.0, rounds: int = 2) -> dict[str, float]:
        """Request each parameter by name. Returns what was actually received.

        Missing parameters are normal, not an error: SERVO9_FUNCTION exists on a vehicle
        with nine outputs and not on one with four, and detectors already treat absence as
        "unknown" rather than zero.
        """
        deadline = time.time() + timeout

        for _ in range(rounds):
            outstanding = [n for n in self.names if n not in self.values]
            if not outstanding:
                break

            for name in outstanding:
                self.conn.mav.param_request_read_send(
                    self.conn.target_system, self.conn.target_component,
                    name.encode("utf-8"), -1,
                )

            while time.time() < deadline:
                msg = self.conn.recv_match(type="PARAM_VALUE", blocking=True, timeout=1.0)
                if msg is None:
                    break
                pid = msg.param_id
                if isinstance(pid, bytes):
                    pid = pid.decode("utf-8", "ignore")
                self.values[pid.rstrip("\x00")] = float(msg.param_value)
                if all(n in self.values for n in self.names):
                    break

            if all(n in self.values for n in self.names):
                break

        # "Complete" means the threshold parameters arrived. SERVO*_FUNCTION gaps are
        # expected and the actuator detector falls back to scanning C1..C8.
        self.complete = all(n in self.values for n in ACTUATOR_PARAMS + BATTERY_PARAMS)
        return self.values

    def as_dict(self) -> dict[str, float]:
        return dict(self.values)
