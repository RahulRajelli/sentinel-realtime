# HANDOFF — state as of 2026-08-15 (end of session)

Read this first in a new session. `START-HERE.md` is the shorter version and names the one job.
Technical state only; career/priority context lives in `../plans/FOCUS.md`, not in this repo.

**One sentence:** the durability work is finished and the archive is sound, but the science still
rests on one fault and three flights, and every claim about *agents* turned out to be a property
of one model.

```bash
../ardupilot-log-analyzer/.venv/Scripts/python.exe -m pytest -q   # 181 tests, all offline
python scripts/manifest.py --verify                               # 28/28 bundles intact
```

---

## 1. What is established

### The robust result

> **`compass_offset` defeats the deterministic baseline. B0 = 0.00 in every run, every
> configuration, both model families. Ten-plus runs, zero exceptions.**

`compass.py:45 MIN_ANOMALY_S = 1.0` makes the compass detector wait a full second while `ekf.py`
fires on threshold crossing, so the symptom is advised at 8.8 s and the cause at 9.8 s. B0's rule
is "first advisory after injection is the root cause", so it is wrong by construction and cannot
be tuned out of it. This is what the project set out to build.

### Cross-model: the ranking INVERTS

3 bundles x 3 prompt variants, 5 independent runs per cell, bundle-level scoring.

| judge | gemini-2.5-flash | gpt-5.6-sol |
|---|---|---|
| B0 deterministic | 0.00 | 0.00 |
| B1 single-shot | **0.89** | 0.71 |
| B3 tool agent | 0.67 | **0.96** |

**On gemini the agent loses to one prompt; on gpt it wins.** "The tool-using agent loses to
single-shot" is a property of gemini-2.5-flash, not of agents.

### The timestamp effect is also gemini-specific

Tool count held at 2; only the presence of `t` varies. Five runs per cell.

| | timed | untimed |
|---|---|---|
| gpt-5.6-sol | **1.00** (symptom-as-root 0/9) | 0.96 (0/9) |
| gemini-2.5-flash | 0.11 (symptom-as-root **8/9**) | 0.67 (1/9) |

gpt never names a symptom as root in any configuration and is marginally *better* with
timestamps. So "give a diagnostic model evidence, not the alarm log" does not generalise.

> **OPEN DECISION.** `SPECS` is the timestamp-free surface, set on gemini-only evidence, and it
> costs gpt 1.00 -> 0.96. Revert it, keep it as the safer floor, or select per model. **Do not
> flip it again without measuring both models.**

### Fabrication is not the failure mode

`check_rationale_grounding` checks measurements quoted in the rationale PROSE against everything
the flight recorded. Across **595 verdicts, four judges, two model families: 2 ungrounded quotes,
0.34%.**

The case that motivated it -- gemini writing *"at 9.062s"* -- turns out to be a **real** advisory
time. **The models are not inventing numbers; they draw wrong conclusions from real ones.** No
grounding check catches that. Only a scenario where the correct and the plausible answer differ
does, which is what `compass_offset` is.

Reported, not scored, by default. `strict_rationale=True` gates on it.

### Run-to-run variance

Gemini is not deterministic at temperature 0 / seed 0. Five identical runs: B1 `.89 .89 .89 .89
1.00`, B3 `.78 .67 .67 .67 .67`. **Spread 0.11, one judgement in nine.** B0 returned exactly 0.00
five times, as deterministic code must.

**Always run five and report a mean.** Single runs produced two retracted claims here.

---

## 2. Five claims retracted, and what caught each

The pattern is the finding: every retraction came from a check, none from an argument.

| claim | why it was wrong | caught by |
|---|---|---|
| "the difference is significant" | Wilson on n=27 judgements for 9 independent flights. At n=9 they overlap | external review (gemini-2.5-pro) |
| "the fix reached 0.96 / 0.89" | one run, and it was the outlier. Five-run mean is 0.69 | the variance study |
| "agents read order as causality" | true of gemini, false of gpt | the cross-model runs |
| "the residual gap is a scoring bug" | fixed the bug; the gap did not move | the citation re-run |
| "remove timestamps from the tools" | helps the weak model, slightly hurts the strong one | the gpt timed arm |

Statistics are reported per BUNDLE (`ci_bundle`), never per judgement.

---

## 3. Architecture

### Three context tiers

| tier | span | what |
|---|---|---|
| live | 120 s | `RollingBuffer`, within-flight detection |
| bundle | one flight | frozen, hash-fingerprinted file |
| history | 30-day window | `memory.py`, append-only JSONL per airframe |

`prior_incidents` answers "third compass anomaly in eight flights". Opt-in:
`--history history/flights.jsonl --offer-tools prior_incidents`. It stores counts and dates, never
the earlier flights; excludes the flight under judgement from its own history; and carries no
ground truth. `airframe_id` is **excluded from `bundle_id`** -- provenance, not content.

### Silence now has three distinguishable causes

This was one ambiguous signal and is now three:

| a quiet screen means | reported by |
|---|---|
| nothing is wrong | both green |
| nobody was listening | `coverage.py` -- detector preconditions |
| the monitor fell behind | `health.py` -- link stall, packet loss, cycle overrun |

`detect_oscillation` returns nothing below 7 Hz of ATT, with no commanded attitude, or under 20
samples. Each is correct and each was silent. `health.py` is reported, never corrective -- a
monitor that quietly repairs itself is one whose degradation you learn about after the flight.

### Integrity

`SCHEMA_VERSION = 2` with a migrator, so an old capture upgrades instead of being orphaned. Three
identities are legitimate in this archive: `(v1, timing included)`, `(v1, timing excluded)`,
`(v2, timing excluded)`. Migration proves authenticity against all three and **nothing else**, so
it cannot launder a tampered file.

`manifest.py` adds full-length SHA-256 over file bytes plus an optional HMAC. It catches edits
`bundle_id` is designed to ignore (`airframe_id`, coverage, health). **It is not a signature** --
HMAC is a shared secret, so anyone who can verify can also forge.

### Optional judge tools (never defaults)

`prior_incidents`, `signal_trajectory`, `detector_coverage`, plus the four retired time-bearing
tools. Offered with `--offer-tools`. `signal_trajectory` is bounded by construction: 1,056
samples render to 863 characters, and 999 buckets clamp to 60.

> **`prior_incidents` and `signal_trajectory` are unmeasured.** No scenario repeats a fault on
> one airframe, and trajectory has never been offered to a live model. Capabilities, not results.

---

## 4. Detector state — measured across 27 flights

| detector | detections | advisories | status |
|---|---|---|---|
| `accel_clipping` | 6,937 | 17 | proven |
| `vibration_excessive` | 3,734 | 20 | proven |
| `actuator_saturation` | 1,777 | 23 | proven |
| `ekf_inconsistency` | 515 | 8 | proven |
| `compass_inconsistency` | 459 | 6 | proven |
| `gps_fix_loss` | 63 | 5 | proven |
| `gps_high_hdop` | 0 | 0 | **unreachable**: `SIM_GPS_UBLOX.cpp:284` hardcodes hDOP 1.21 |
| `control_oscillation` | 0 | 0 | **unreachable**: tracking error caps ~2.44 deg vs a 3.0 threshold |
| `battery_*` | 0 in SITL | — | fired once on a **real** log; never in simulation |

13,485 raw detections collapse to 79 advisories -- the escalation gate working. Worst cycle
7.6-29.6 ms of a 1,000 ms budget. Zero false positives before injection in every run.

**Six advisory types is the vocabulary ceiling.** A fault outside it produces silence, and that
miss rate is unmeasurable with injected faults.

### Scenarios

`null`, `vibration`, `gps_loss`, `wind` (base four, 12/12 pass; `null` and `wind` are the
hallucination controls and score 1.00 for all judges). **`compass_offset` is the only
discriminating fault.** `stiff_airframe` is RETIRED and `hot_gains` is BLOCKED, both with the
measurements in their scenario comments.

`replay_2024-04-30` loads again after the migration and reproduces **346 detections -> 11
advisories = 96.82% suppressed**. That number is citeable again.

---

## 5. Do this next

**Stage 1 (durability) is complete**: detector coverage, schema migration, monitor self-health,
tamper-evidence. None of it moved an accuracy number -- it makes the existing results
trustworthy, not broader.

**Stage 2 is where accuracy lives:**

1. **New detectors with genuine persistence gates.** The single highest-value change: it fixes
   the six-word vocabulary AND the missing second discriminating scenario at once. Only
   `compass.py` and `oscillation.py` have time gates today and one is unreachable, which is
   exactly why there is one working ambiguous pair.
2. **Disagreement as a signal.** Two models already rank judges in opposite directions. In
   production, models disagreeing is when a human should look. No new science needed.
3. **Labelled real flight logs.** Every "proven" detector is proven against faults that were
   injected and labelled here. This is data acquisition and it is worth more than the rest.

**Open decisions, not work:** the default tool surface (section 1); whether to loosen
`rubrics/explanatory-prose.md`, whose A2 "zero undefined terms" is unreachable and stalled the
whitepaper at 54/100; the LinkedIn angle; and whether to publish the whitepaper as a page.

---

## 6. Environment

```bash
# The analyzer venv. System Python has no deps.
../ardupilot-log-analyzer/.venv/Scripts/python.exe -m pytest -q

# SITL lives in WSL, needed only to capture new flights.
wsl -d Ubuntu-24.04 -- bash -c "ls /root/ardupilot/build/sitl/bin/"
```

**Models.** Gemini via Google ADC (`--provider gemini`). Everything else via the
OpenAI-compatible client against the llmapi gateway (~389 models, `claude-opus-5` included):

```bash
export OPENAI_BASE_URL="https://api.llmapi.ai/v1"
export OPENAI_API_KEY=...     # opencode stores it under `llmapi` in auth.json
python scripts/e4_judge.py --bundles bundles --only compass_offset \
  --judges B0,B1,B3 --provider openai --model gpt-5.6-sol --out verdicts.json
```

**No Anthropic key exists on this machine and none is needed.**

Rules that are not preferences:

1. **Name the model** in anything published. Two models rank the judges in opposite directions,
   so an unnamed finding may simply be false.
2. **Never quote a single run.** Five repeats, mean and spread.
3. **Never edit code while an experiment is running.** It crashed a sweep and left five runs
   straddling a scoring change; the whole set was discarded.
4. **Quote `ci_bundle`**, not the judgement-level interval.
5. **Never lower a detector threshold to make a scenario pass.** Three faults are recorded as
   unreachable rather than tuned into existence.
6. Console output stays ASCII. Use `-u` on backgrounded Python.
7. `pkill -f arducopter` from `wsl bash -c` kills its own shell.

---

## 7. Decisions already made — do not re-litigate

| Decision | Why |
|---|---|
| Capture and judgement are separated | Fly once, freeze a `RunBundle`, judge offline |
| `bundle_id` excludes wall-clock timings | It must fingerprint the flight, not the host |
| `airframe_id`, coverage, health excluded from identity | Observing conditions, not what the aircraft did |
| Transport failures degrade, never crash | Attributed to HARNESS, never to the model |
| Replay sets no ground-truth label | Deriving it grades the system against its own opinion |
| No RAG, no vector DB | There is no corpus |
| **No LLM anywhere near flight control** | Verified: no model client is imported in `runner.py`, `capture.py` or `gate.py` |
| `detector_evidence` is capped | Unbounded it returned 191,465 chars in one call |
| Citations accept a value anchor | Verified against recorded evidence, so not a weaker rule |
| History stores counts, not flights | Four prior bundles would quadruple the judge's input |
| Rationale grounding reports, does not gate | Gating would change what every earlier number means |
| Health is reported, never corrective | Self-repair hides degradation until after the flight |

**Changing `_identity_payload` or `_TIMING_FIELDS` must bump `SCHEMA_VERSION` and add a migrator
in the same commit.** A test fails if a version has no migrator.

## 8. Known-broken

| Thing | Status |
|---|---|
| `coax`, `tilthvec`, `dodeca-hexa` | not flyable as configured |
| quadplane | boots and arms, peak 0.0 m -- needs tilt-servo config |
| `judges/llm.py` (Anthropic SDK) | written, tested, never run. **Not a blocker** -- use the gateway |
| `judges/grader.py`, kappa | written, never run |
| `gps_high_hdop`, `control_oscillation` | unreachable in SITL, measured |
| PX4 | unsupported by design -- see `docs/SETUP.md` |

## 9. Raw evidence

```
results/isolation/     gemini timestamp isolation, 5 runs per arm
results/crossmodel/    gpt-5.6-sol, both arms, 5 runs each
results/citation-fix/  gemini after the value anchor
variance/              gemini five-repeat variance
verdicts_*.json        the 9- and 22-bundle sweeps
bundles/MANIFEST.json  full-length digests over all 28 captures
```

Every figure regenerates with `scripts/e4_report.py`. Docs: `README.md` (what it is),
`docs/SETUP.md` (running it on your own aircraft), `WHITEPAPER.md` (the write-up).
