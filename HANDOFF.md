# HANDOFF — state as of 2026-08-14

Read this first in a new session. It is the technical state only; the career/priority context
lives in `../plans/FOCUS.md`, which is private and not in this repo.

**The one sentence that matters:** everything is built and tested, the agent has been run live
for the first time, and it **tied with the free baseline** — because the fault set cannot
separate them. The next run is the one that can.

---

## 1. What is true right now

| | State |
|---|---|
| Code | Complete through the whole judge pipeline. **76 tests, all offline** (no simulator, network or key) |
| CI | Green — `tests` + `codeql` on every push |
| Public | [sentinel-realtime](https://github.com/RahulRajelli/sentinel-realtime) · [ardupilot-log-analyzer](https://github.com/RahulRajelli/ardupilot-log-analyzer), both Apache-2.0 |
| Bundles captured | **16** in `bundles/` — 12 SITL (4 scenarios × 3 reps), 3 airframe motor-out, 1 real-log replay |
| Live LLM | **Working via Gemini on Vertex** using existing Google ADC. Anthropic path has no key |

### Measured, and reproducible

- SITL re-fly: **12/12 scenarios pass**, 0 false positives, root cause advised first, latency
  1.0 s (vibration) / 5.0 s (GPS loss), worst cycle 7.6–27.7 ms of 1000 ms.
- Real-log replay: 140 cycles, 346 raw detections → 11 advisories, **96.8% suppressed**.
  The gate measured **96.9% on injected SITL faults** — independent data, agreeing to 0.1 points.
- Airframes, identical motor-out injection: **quad** → saturation + vibration + EKF (critical);
  **hexa** → saturation only (critical); **octa** → saturation only (**warning**). Cascade
  shrinks and severity drops as redundancy rises.

### The finding that governs what to do next

**B0 (free, deterministic, 0 tokens) has won or tied every comparison so far.**

```
bundle             B0                    B3 agent (Gemini)     B0  B3   tokens
quad_motor_fail    actuator_saturation   actuator_saturation    1   1     2494  (degraded)
hexa_motor_fail    actuator_saturation   actuator_saturation    1   1    10904
octa_motor_fail    actuator_saturation   actuator_saturation    1   1     5467
```

`stats.ambiguity_worked()` flags this automatically: *"B0 scores 100%: no judge can beat it on
this fault set, so any agent result is a tie at higher cost."*

The cause is the fault library, not the code. In every fault captured so far the root cause's
own detector fires first, so "first advisory" and "root cause" never come apart — and that gap
is the only thing the agent has to be better at.

---

## 2. Do this first

Fly the two ambiguous pairs. They are already defined in `scripts/r7_r8_scenarios.py` and have
never been flown:

```bash
python scripts/r7_r8_scenarios.py --scenarios compass_offset,stiff_airframe --bundles bundles --repeat 3
```

- **`compass_offset`** is the one to trust. A magnetometer offset raises EKF variance at once,
  but `compass.py:45 MIN_ANOMALY_S = 1.0` makes the compass detector wait a full second. The
  symptom leads the cause **by a constant in the source**, not by tuning.
- **`stiff_airframe`** needs a tuning sweep on `SIM_ACC1_RND` (start 90, then 70/110). Accept the
  first value where `ambiguity_confirmed` is true across 3 flights.

Then judge them. On these, B0 should be **confidently wrong** — that is the first run that can
produce a real result rather than a tie.

---

## 3. Environment — the things that will waste your time

```bash
# Use the analyzer venv. The system Python has no pytest and no deps.
../ardupilot-log-analyzer/.venv/Scripts/python.exe -m pytest -q

# SITL lives in WSL. Binaries at /root/ardupilot/build/sitl/bin/
wsl -d Ubuntu-24.04 -- bash -c "ls /root/ardupilot/build/sitl/bin/"   # arducopter, arduplane
```

- **LLM auth:** `ANTHROPIC_API_KEY` is **not set** and Anthropic-on-Vertex has **zero quota**
  (429 on every Claude model, on an authenticated request). Gemini on Vertex works today via
  ADC, project `gen-lang-client-0725459099`, location `global`. To switch back to Claude, set
  the key and use `sentinel/judges/llm.py` — it is written and tested, just unrun.
- **Anything published from a Gemini run must name the model.** The table measures Gemini, not
  Claude.
- Python buffers stdout to a file; use `-u` on background SITL runs or the log stays empty and
  looks hung when it is fine.
- Console output must stay ASCII — Windows cp1252 renders anything else as `?`.

---

## 4. Decisions already made — do not re-litigate

| Decision | Why |
|---|---|
| **Capture and judgment are separated** | 4 min per SITL flight vs ~96 judgments. Fly once, freeze a `RunBundle`, judge offline |
| `bundle_id` excludes wall-clock timings | Found by replaying one log twice and getting two ids. It must fingerprint the flight, not the host |
| Refactor gate splits *concluded* vs *observed* fields | Recalibrated 2026-08-14 **after it failed**; reasoning and the raw numbers are in `capture.py`. Verified it still catches symptom-as-root, false positives, suppression collapse, doubled latency |
| Transport failures degrade, never crash | A single 429 was killing whole sweeps. Attributed to HARNESS, never to the model |
| Replay sets **no** ground-truth label | Deriving it from detector output would grade the system against its own opinion |
| No RAG, no vector DB | There is no corpus |
| No LLM near flight control | Judges are offline; the bundle is a frozen file, so read-only is structural |

## 5. Known-broken, with the actual reason

| Thing | Status |
|---|---|
| `coax` airframe | **Not a SITL model.** `--model coax` → *"Vehicle model (coax) not found"*. `copter-coax.parm` only sets FRAME_CLASS for the flight code; the physics layer has no coaxial model |
| `tilthvec` ("vtol") | Boots, **no heartbeat**. Not flyable as configured |
| `dodeca-hexa` | Boots, **will not arm** |
| **quadplane** | arduplane **is built** (needed `empy==3.3.4`). `--model quadplane` boots, arms, accepts `NAV_VTOL_TAKEOFF`, but **peak 0.0 m** — the `quadplane-tilttri` defaults need tilt-servo config |
| `judges/llm.py` (Anthropic) | Written and unit-tested, **never run live** — no key |
| `judges/grader.py`, κ | Written, **never run** — needs a live model across a full sweep |

## 6. What is built but never exercised end to end

`scripts/e4_judge.py` and `scripts/e4_report.py` have only been run with `--dry-run` and with a
hand-written loop. The full sweep — B0/B1/B2/B3 × 3 prompt variants × N bundles, with B2's `k`
derived from B3's measured spend — has **not** run. Do it after the ambiguous pairs exist,
because running it now would just produce a table of ties.

```bash
python scripts/e4_judge.py --bundles bundles --dry-run          # free, shows the table shape
python scripts/e4_report.py --bundles bundles --verdicts verdicts.json --markdown
```

## 7. Product surface, for reference

```bash
sentinel doctor                            # what is installed, what is missing, how to fix it
sentinel analyze FLIGHT.BIN --html r.html  # a log you already have -> findings + emailable report
sentinel replay FLIGHT.BIN                 # real flight through the realtime tier -> a bundle
sentinel watch --conn COM5,57600 --passive # live, without touching stream rates
```

`analyze` on a real log correctly flagged `BATT_LOW_VOLT = 10.5 V` on a 22.2 V pack — a 3S
failsafe left on a 6S battery, which would never have fired. That is the best demo in the repo.
