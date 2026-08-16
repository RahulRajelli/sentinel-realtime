#!/usr/bin/env python3
"""ONE flight. What the autopilot said, and what the detectors advised, on one clock.

    python -u scripts/side_by_side.py --scenario hot_gains_lowd
    python -u scripts/side_by_side.py --scenario hot_gains_lowd --out docs/_side-by-side.json

WHY THIS EXISTS, AND WHAT IT REPLACES.

`ardupilot_says.py` records what ArduPilot broadcasts. `r7_r8_scenarios.py` records what the
detectors advise. Both are honest, and putting their two outputs next to each other is not,
because they come from different flights. A claim of the form

    "ArduPilot said X at +7.9 s and my detector said Y at +4.8 s"

reads as one aircraft and is two. Every individual number in it can be true while the sentence
is false. That is a harder error to catch than a wrong number, because per-fact checking passes.

So this flies once and records both streams from the same connection, stamped with the same
runner clock:

  * STATUSTEXT from the vehicle -- what a pilot reads in the Mission Planner message pane
  * advisories out of the escalation gate -- what this project would tell them instead

It answers the first objection a flight-controller engineer raises, which is not "does your
detector work" but "ArduPilot already warns me, so what does yours add?" The answer has to be a
transcript, not a paragraph.

HONEST LIMITS, because they decide what the output may be used for:

  * n = 1 flight. This produces an anecdote with a timestamp, not a distribution. Fly it several
    times before quoting a gap to two decimal places.
  * SITL is not an airframe. Real thrust loss involves damaged geometry, and the simulator's
    thrust-loss check fires off modelled physics.
  * The two streams share a clock but not a definition of zero. Injection time is the origin
    used below, because that is the only instant both halves agree on.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_FLIGHTDX_SRC = _ROOT.parent / "ardupilot-log-analyzer" / "src"
if _FLIGHTDX_SRC.exists():
    sys.path.insert(1, str(_FLIGHTDX_SRC))

from pymavlink import mavutil  # noqa: E402

from sentinel.gate import EscalationGate  # noqa: E402
from sentinel.runner import LiveRunner  # noqa: E402

sys.path.insert(0, str(_ROOT / "scripts"))
from ardupilot_says import SCENARIOS, connect, launch_sitl, set_param  # noqa: E402
from r7_r8_scenarios import arm_and_takeoff  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="hot_gains_lowd", choices=sorted(SCENARIOS))
    ap.add_argument("--out", default="docs/_side-by-side.json")
    ap.add_argument("--settle", type=float, default=8.0, help="seconds airborne before injecting")
    ap.add_argument("--watch", type=float, default=32.0, help="seconds to record after injecting")
    ap.add_argument("--cadence", type=float, default=0.25,
                    help="detector cadence. 0.25 matches the ambiguous-pair scenarios, where 1 Hz "
                         "puts two events 300 ms apart into the same cycle")
    args = ap.parse_args()

    cfg = SCENARIOS[args.scenario]
    if not cfg["fly"]:
        print(f"{args.scenario} is a ground-observable fault; this probe is for flying ones.")

    print(f"launching SITL -> tcp:127.0.0.1:{5760 + 10 * 4}")
    proc = launch_sitl()
    conn = connect()
    print("heartbeat received")

    runner = LiveRunner(conn, cadence_s=args.cadence, window_s=120.0)
    runner.request_streams(rate_hz=10)
    runner.fetch_params()

    if cfg["fly"]:
        print("arming and taking off -- the fault means nothing on the ground")
        arm_and_takeoff(conn, alt=10.0)
        print("airborne")

    gate = EscalationGate()
    said: list[dict] = []       # what the autopilot broadcast
    advised: list[dict] = []    # what the gate raised
    injected_at: float | None = None

    def on_message(msg, t: float) -> None:
        if msg.get_type() != "STATUSTEXT":
            return
        txt = msg.text if isinstance(msg.text, str) else msg.text.decode(errors="replace")
        said.append({"t": round(t, 3), "sev": int(msg.severity), "text": txt.strip()})

    def on_cycle(r) -> None:
        nonlocal injected_at
        if injected_at is None and r.t >= args.settle:
            injected_at = round(r.t, 3)
            print(f"\n--- t={injected_at:.2f}s  {cfg['banner']} ---")
            for k, v in cfg["inject"].items():
                set_param(conn, k, v)
            return
        for adv in gate.submit(r.incidents, r.t):
            inc = adv.incident
            ev = "; ".join(f"{e.metric}={e.value:g} (thr {e.threshold:g})"
                           for e in inc.evidence[:2])
            advised.append({"t": round(r.t, 3), "type": inc.type,
                            "sev": inc.severity, "reason": adv.reason, "evidence": ev})
            print(f"  [{r.t:7.2f}] ADVISORY  {inc.type}[{inc.severity}]")

    runner.run(args.settle + args.watch, on_cycle=on_cycle, on_message=on_message)

    if injected_at is None:
        print("\nNOTHING WAS INJECTED -- the run ended before the settle time elapsed.")
        proc.terminate()
        return 1

    # Everything below is relative to injection, the only instant both halves agree on.
    rel_said = [dict(e, rel=round(e["t"] - injected_at, 3)) for e in said
                if e["t"] >= injected_at]
    rel_adv = [dict(e, rel=round(e["t"] - injected_at, 3)) for e in advised
               if e["t"] >= injected_at]

    print("\n" + "=" * 78)
    print(f"ONE FLIGHT, TWO STREAMS  ({args.scenario})")
    print("=" * 78)
    print("\n  WHAT ARDUPILOT SAID")
    for e in rel_said or []:
        print(f"    +{e['rel']:6.2f}s  {e['text']}")
    if not rel_said:
        print("    (nothing)")
    print("\n  WHAT THE DETECTORS ADVISED")
    for e in rel_adv or []:
        print(f"    +{e['rel']:6.2f}s  {e['type']}[{e['sev']}]  {e['evidence']}")
    if not rel_adv:
        print("    (nothing)")

    first_said = rel_said[0]["rel"] if rel_said else None
    causes = [e for e in rel_adv if e["type"] == "control_oscillation"]
    first_cause = causes[0]["rel"] if causes else None
    if first_said is not None and first_cause is not None:
        print(f"\n  cause advised at +{first_cause:.2f}s; autopilot's first word at "
              f"+{first_said:.2f}s. Same flight, same clock.")
    print("\n  Neither stream RANKS its own output. That is the gap this project measures.")

    out = _ROOT / args.out
    out.write_text(json.dumps({
        "scenario": args.scenario, "injected_at": injected_at, "inject": cfg["inject"],
        "cadence_s": args.cadence, "one_flight": True,
        "said": rel_said, "advised": rel_adv,
    }, indent=1))
    print(f"\nwrote {out.relative_to(_ROOT)}")
    proc.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
