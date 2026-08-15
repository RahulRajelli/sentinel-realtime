# HANDOFF — state as of 2026-08-15

Read this first in a new session. `START-HERE.md` is the shorter version for a fresh agent.
Technical state only; career/priority context lives in `../plans/FOCUS.md`, not in this repo.

**The one sentence that matters:** the ambiguous fault works and reliably defeats the free
baseline on every model tested — but every claim built on top of it about *agents* turned out to
be a property of one model, and was retracted.

---

## 1. What is actually established

### The robust result

> **`compass_offset` defeats the deterministic baseline. B0 = 0.00, in every run, in every
> configuration, across two model families. Ten-plus runs, zero exceptions.**

This is the thing the project set out to build: a fault where "first alarm" and "root cause" come
apart. `compass.py:45 MIN_ANOMALY_S = 1.0` makes the compass detector wait a full second while
`ekf.py` fires on threshold crossing, so the symptom is advised at 8.8 s and the cause at 9.8 s —
a gap equal to the constant, reproduced in every flight. B0's rule is "first advisory after
injection is the root cause", so B0 is wrong by construction and cannot be tuned out of it.

Everything below is weaker than this.

### Cross-model: the tier ranking INVERTS

`compass_offset`, 3 bundles x 3 prompt variants, 5 independent runs per cell, bundle-level scoring.

| judge | gemini-2.5-flash | gpt-5.6-sol |
|---|---|---|
| B0 deterministic | 0.00 | 0.00 |
| B1 single-shot | **0.89** | 0.71 |
| B3 tool agent | 0.67 | **0.96** |

**On Gemini the agent loses to one well-formed shot. On GPT it wins.** So "the tool-using agent
loses to single-shot" is NOT a property of agents; it is a property of gemini-2.5-flash. An
earlier version of this file claimed otherwise and was wrong.

### Cross-model: the timestamp effect is Gemini-specific

Tool count held at 2 in both arms; the ONLY difference is whether the evidence carries `t`.
Five runs per cell.

| | timed (`detector_evidence`) | untimed (`evidence_untimed`) |
|---|---|---|
| **gpt-5.6-sol** | **1.00** (symptom-as-root 0/9) | 0.96 (0/9) |
| **gemini-2.5-flash** | 0.11 (symptom-as-root **8/9**) | 0.67 (1/9) |

On Gemini, removing timestamps is transformative: 0.11 -> 0.67, symptom-as-root 8/9 -> 1/9, with
zero spread across five runs. **On GPT it does nothing** — GPT never names a symptom as the root
cause in any configuration, and is marginally *better* with timestamps.

So the design rule this file previously asserted — *"detection-order metadata causes
symptom-as-root; give a diagnostic model evidence, not the alarm log"* — **does not generalize.**
The accurate statement is: *a weaker model conflated detection order with causality, and a
timestamp-free evidence tool fixes that model. A stronger model reads the same ordering and draws
the correct inference.*

> **OPEN DECISION.** `SPECS` was changed to the timestamp-free surface on Gemini-only evidence
> (commit `16e030a`). For GPT that default is a small regression (1.00 -> 0.96). Options: revert
> to the timed surface with `evidence_untimed` documented as a mitigation for weaker models; keep
> untimed as the safer floor (Gemini 0.67 vs 0.11); or select per model. **Not decided — do not
> flip it again without measuring both models.**

### Run-to-run variance — measure it, never quote one run

Gemini is not deterministic at `temperature=0 / seed=0`. Five identical runs on identical
bundles: B1 `.89 .89 .89 .89 1.00`, B3 `.78 .67 .67 .67 .67` — **spread 0.11, one judgement in
nine.** B0 returned exactly 0.00 five times, as deterministic code must.

**Always run five and report a mean.** Single-run numbers produced two retracted claims here.

---

## 2. Five claims retracted on 2026-08-15, and what caught each

Recorded because the pattern is the finding: every retraction came from a check, none from an
argument.

| claim | why it was wrong | caught by |
|---|---|---|
| "B1's CI does not overlap B3's, so this is not noise" | Wilson computed on n=27 judgements for 9 independent flights — pseudo-replication, intervals ~sqrt(3) too narrow. At n=9 they overlap | external review (gemini-2.5-pro) |
| "the fix took B3 to 0.96 / compass 0.89" | single run; that run was the outlier. Mean over five is 0.69 | 22-bundle sweep, then the variance study |
| "the agent reads detection order as causality" | true of gemini-2.5-flash, false of gpt-5.6-sol | the GPT cross-model runs |
| "the entire residual B3-vs-B1 gap is the citation defect" | arithmetic projection (0.67 + 2/9) across a nondeterministic output. Fixing citations changed attribution but not accuracy | the citation-fix re-run |
| "the fix is a tool that returns evidence without timestamps" | Gemini-only. GPT is unaffected and slightly prefers timestamps | the GPT timed arm |

Statistics are now reported per BUNDLE (`ci_bundle`), never per judgement. The three prompt
variants of a flight are repeated measures, not independent trials.

---

## 3. The citation fix — works, but not for the reason predicted

`Citation` accepts **either** a timestamp **or** a value, and requires at least one. A value
anchor is verified against evidence actually recorded for that metric, so a fabricated number
fails exactly as a fabricated timestamp does; a citation with neither anchor is rejected.

It exists because `evidence_untimed` removes every timestamp, so a judge using it could not
produce a valid citation at all — 2 of 9 agent verdicts per run named the CORRECT root cause and
scored zero for it.

**Measured before/after on Gemini:** `no-citation [2,2,2,2,2] -> [0,0,0,0,0]`, misses reattributed
from **harness** to **model**, accuracy **unchanged at 0.67**. The fix makes failures honestly
attributable — which is the scoring layer's whole job — but it did not close the gap. The
prediction that it would was a projection across a nondeterministic output, and it was wrong.

---

## 4. Scenario library

| scenario | state |
|---|---|
| `null`, `vibration`, `gps_loss`, `wind` | base four, re-flown 2026-08-14, 12/12 pass |
| **`compass_offset`** | **the only discriminating fault.** Works 3/3, structurally guaranteed |
| `hot_gains` | pair C, BUILT and BLOCKED |
| `stiff_airframe` | RETIRED, kept as the record |

**Hallucination controls pass:** `null` and `wind` score 1.00 for all four judges — no judge
invented a fault on a clean flight.

### Three root causes SITL cannot produce

Two build plans have now assumed otherwise, so this is stated plainly with the measurements:

| root cause | why unreachable |
|---|---|
| `gps_high_hdop` | `SIM_GPS_UBLOX.cpp:284` hardcodes hDOP = 1.21, below the 2.0 threshold, and no SIM parameter touches it. Confirmed in our own data: never once fired |
| `control_oscillation` | tracking error is bounded ~2.44 deg against a 3.0 threshold over 8 configurations. Gains are the frequency lever, not the amplitude one, and in guided flight ATTITUDE_TARGET leans into the wind so the error stays bounded |
| `vibration_excessive` *as an ambiguous pair* | `SIM_ACC1_RND` moves peak and RMS together; the two detectors key off one each, so no value separates them |

**Do NOT fix any of these by lowering a detector threshold.** That makes the experiment pass by
redefining the fault.

**`compass_offset` is the only discriminating scenario, so every comparison rests on 3 flights.**
That is the single biggest weakness in the evidence. `compass.py` and `oscillation.py` hold the
only two time gates in the detector set and one is unreachable — a second ambiguous pair probably
needs a new DETECTOR, not a new scenario.

---

## 5. Do this next

1. **Decide the default tool surface** (section 1). It is currently set from one model's evidence.
2. **A second discriminating scenario.** Everything else is refinement; this is the only thing
   that raises n above 3 flights.
3. **More models, now cheap.** The `llmapi` gateway exposes ~389 models including
   `claude-opus-5`, `claude-sonnet-5`, `qwen3.8-max`, `kimi-k3`. Two models showed opposite
   rankings; a third and fourth would show whether that is a capability gradient or noise.
4. **Regenerate the replay bundle.** `replay_2024-04-30 17-30-57.json` needs its original `.BIN`,
   which is not in the repo. It is the only evidence behind the 96.8% suppression claim.

---

## 6. Environment

```bash
# Use the analyzer venv. The system Python has no deps.
../ardupilot-log-analyzer/.venv/Scripts/python.exe -m pytest -q     # 118 tests, all offline

# SITL lives in WSL, needed only to capture new flights.
wsl -d Ubuntu-24.04 -- bash -c "ls /root/ardupilot/build/sitl/bin/"
```

**Model access.** Gemini works via Google ADC (`--provider gemini`). Everything else goes through
the OpenAI-compatible client:

```bash
export OPENAI_BASE_URL="https://api.llmapi.ai/v1"
export OPENAI_API_KEY=...        # opencode stores it under `llmapi` in auth.json
python scripts/e4_judge.py --bundles bundles --only compass_offset \
  --judges B0,B1,B3 --provider openai --model gpt-5.6-sol --out verdicts.json
```

There is **no Anthropic key on this machine and none is needed** — `claude-opus-5` and
`claude-sonnet-5` are reachable through the same gateway with `--provider openai`.

- Anything published must NAME THE MODEL. `client.name` records model @ host into the verdict file.
- Console output must stay ASCII — Windows cp1252 renders anything else as `?`.
- Use `-u` on backgrounded Python or the log stays empty and looks hung.
- `pkill -f arducopter` from `wsl bash -c` kills its own shell (self-match, exit 15).
- **Never edit code while an experiment is running.** Doing so on 2026-08-15 crashed one sweep
  mid-run and left five runs straddling a scoring change; the whole set had to be discarded.

---

## 7. Decisions already made — do not re-litigate

| Decision | Why |
|---|---|
| Capture and judgement are separated | 4 min per flight vs ~96 judgements. Fly once, freeze a `RunBundle`, judge offline |
| `bundle_id` excludes wall-clock timings | It must fingerprint the flight, not the host |
| Transport failures degrade, never crash | Attributed to HARNESS, never to the model |
| Replay sets **no** ground-truth label | Deriving it from detector output grades the system against its own opinion |
| No RAG, no vector DB | There is no corpus |
| No LLM near flight control | Judges are offline; the bundle is a frozen file |
| `detector_evidence` is capped | Unbounded, it returned 191,465 chars (~48k tokens) in one call and degraded B3 on contact |
| Citations accept a value anchor | `evidence_untimed` removes timestamps; verified against recorded evidence, so it is not a weaker rule |

Changing `_identity_payload` or `_TIMING_FIELDS` **must** bump `SCHEMA_VERSION` in the same
commit, or every existing bundle silently fails to load and reports itself as tampered with.

---

## 8. Known-broken

| Thing | Status |
|---|---|
| `coax`, `tilthvec`, `dodeca-hexa` | not flyable as configured |
| quadplane | boots and arms, peak 0.0 m — needs tilt-servo config |
| `judges/llm.py` (Anthropic SDK path) | written, tested, never run. **Not a blocker** — use the gateway |
| `judges/grader.py`, kappa | written, never run |
| `gps_high_hdop` | unreachable in SITL |
| `replay_2024-04-30` bundle | will not load; needs its `.BIN` |

## 9. Product surface

```bash
sentinel doctor                            # what is installed, what is missing, how to fix it
sentinel analyze FLIGHT.BIN --html r.html  # a log you already have -> findings + emailable report
sentinel replay FLIGHT.BIN                 # real flight through the realtime tier -> a bundle
sentinel watch --conn COM5,57600 --passive # live, without touching stream rates
```

`analyze` on a real log correctly flagged `BATT_LOW_VOLT = 10.5 V` on a 22.2 V pack — a 3S
failsafe left on a 6S battery, which would never have fired. Still the best demo in the repo.

## 10. Raw evidence

Every number above regenerates from committed verdict files via `scripts/e4_report.py`:

```
results/isolation/     iso_timed{1..5}, iso_untimed{1..5}    gemini timestamp isolation
results/crossmodel/    gpt2_run{1..5}, gpt_timed{1..5}       gpt-5.6-sol, both arms
results/citation-fix/  cite_fix{1..5}                        gemini, after the value anchor
variance/              var_run{1..5}                         gemini five-repeat variance
verdicts_*.json        the 9- and 22-bundle sweeps
```
