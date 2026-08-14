# HANDOFF — state as of 2026-08-14 (evening)

Read this first in a new session. Technical state only; the career/priority context lives in
`../plans/FOCUS.md`, which is private and not in this repo.

> **Unfinished when this branch was pushed.** A full B0/B1/B2/B3 sweep over 22 bundles (the two
> ambiguous-pair scenarios PLUS the base four, so the table finally has hallucination controls)
> was still running and its output `verdicts_default_v2.json` is NOT in this commit. It is the
> first sweep on the new default tool surface. Re-run it:
>
> ```bash
> python scripts/e4_judge.py --bundles bundles \
>   --only compass_offset,stiff_airframe,null,vibration,gps_loss,wind \
>   --provider gemini --out verdicts_default_v2.json
> python scripts/e4_report.py --bundles bundles --verdicts verdicts_default_v2.json \
>   --only compass_offset,stiff_airframe,null,vibration,gps_loss,wind --markdown
> ```
>
> Every number in section 1 comes from the 9-bundle run and is reproducible from the verdict
> files committed here.

**The one sentence that matters:** the ambiguous pair works and the experiment finally produced a
real result — but the result is that **the tool-using agent ties the free baseline and loses to
the naive single-shot judge**, because its tools show it detection *order* and it mistakes that
for causality.

---

## 1. The measured result

`vertex/gemini-2.5-flash`, 9 bundles x 3 prompt variants, **0 degradations**, matched-spend
control valid at **-7.3%** (inside the +/-10% target for the first time).

**Intervals are per BUNDLE (n=9), not per judgement (n=27).** The three prompt variants of a
flight are repeated measures, not independent trials. See the correction note in section 1a — the
earlier n=27 intervals were ~sqrt(3) too narrow and turned an overlapping comparison into an
apparently significant one.

| judge | accuracy (n=9 bundles) | 95% Wilson | symptom-as-root | tok/judgement |
|---|---|---|---|---|
| B0 deterministic | 0.67 | [0.35-0.88] | 3/9 | 0 |
| **B1 single-shot** | **1.00** | **[0.70-1.00]** | 0/27 | 3,088 |
| B2 k=3 sampling | 1.00 | [0.70-1.00] | 0/27 | 9,092 |
| B3 tool agent | 0.67 | [0.35-0.88] | **9/27** | 9,805 |

**B1 and B3 overlap across [0.70-0.88]. The difference is NOT statistically established** at this
sample size. What IS solid is the mechanism (symptom-as-root 9/27 vs 0/27, a direct count) and
the per-scenario split below — not the significance of the accuracy gap.

Per scenario — only `compass_offset` discriminates:

| scenario | B0 | B1 | B2 | B3 |
|---|---|---|---|---|
| **compass_offset** | **0.00** | **0.89** | **0.89** | **0.00** |
| stiff_airframe | 1.00 | 1.00 | 1.00 | 1.00 |

**Why B3 loses, in its own words.** On the same bundle:

> **B1:** *"High EKF_Magnetometer_Variance indicates issues with the magnetometer data, which is
> the root cause... This unreliable compass data then leads to the EKF becoming inconsistent."*
>
> **B3:** *"The EKF inconsistency was detected **first**... This likely caused the compass
> inconsistency."*

`list_advisories` and `ordering` let B3 re-query when each detector fired. B3 reasoned from that
and adopted B0's rule.

**Correction, 2026-08-14.** This paragraph previously ended *"and B1's CI does not overlap B3's,
so this is not noise"*. That was wrong — it relied on the pseudo-replicated n=27 interval. At the
correct n=9 the intervals overlap and the accuracy difference is not established. The claim now
rests on the symptom-as-root counts and the ablation, not on separated intervals.

It is also NOT true that B1 "cannot see" the ordering: `summarize()` reports advisories with
`t_first` to every judge, B1 included. B1 has the order and does not misuse it. The distinction
that survives is between *seeing* the order once and *querying and elaborating* it.

## 1a. External review, 2026-08-14 — what it caught

Routed to gemini-2.5-pro against a written rubric, instructed to attack rather than confirm.
Verdict **FAIL, 25/100**. Findings accepted, and the fixes are in this file and in the code:

| finding | status |
|---|---|
| Wilson CIs pseudo-replicated (n=27 for 9 independent flights) | **Accepted, fixed** — `stats.py` now reports `ci_bundle`; `e4_report.py` quotes it |
| "monotonic dose-response" overclaims on 3 points, first step not significant | **Accepted** — language removed below |
| Final config confounds timestamp-removal with tool COUNT (5 tools -> 2) | **Accepted** — claim narrowed below |
| "agents read order as causality" rests on one model and one scenario | **Accepted** — every claim now names gemini-2.5-flash and compass_offset |
| Post-hoc storytelling: hypothesis formed from rationales, then confirmed | **Accepted as a risk.** No pre-registration exists. Stated plainly rather than defended |
| B1/B2 numbers do not reconcile with the raw JSON | **Rejected, verified false.** The reviewer recomputed with a naive `root_cause` match and missed `score.py`'s rule that an unresolved citation scores 0. The project's own scorer reproduces 26/27 exactly |

Its own review was flagged `truncated: true`, so it is not a complete pass. Worth re-running
against a smaller input before treating the score as final.

**B2 bought nothing.** Disagreement across k=3: **1 of 27, mean 0.012**. `llm.py` pre-registered
this exact test — if the samples never disagree, B2 is B1 at k times the cost. It is.

### The ablation — why B3 fails, tested rather than inferred

Run with `--withhold-tools`, which removes tools from B3's offered set (removed, not stubbed: a
tool that exists and refuses still tells the model the question is askable).

| B3 configuration | symptom-as-root | compass | stiff | overall | flip | tok/judge |
|---|---|---|---|---|---|---|
| all 5 tools | **9/27** | 0.00 | 1.00 | 0.67 | 0.00 | 9,805 |
| minus `list_advisories`,`ordering` | **7/27** | 0.22 | 1.00 | 0.74 | 0.22 | 9,492 |
| minus all timing-bearing tools | **0/27** | 0.33 | 0.61 | 0.52 | 0.89 | 3,887 |
| **plus `evidence_untimed`** | **1/27** | **0.89** | **1.00** | **0.96** | 0.11 | **5,239** |

Same model, same prompts, same bundles. **Only the tool surface changed.**

**Established:**
1. Symptom-as-root goes 9/27 -> 7/27 -> 0/27 as ordering TOOLS are removed. The endpoints are a
   large, direct count and hold up; the intermediate step (9 -> 7) is not significant on its own.
   Earlier drafts called this a "monotonic dose-response" — **that phrase overclaims on three
   points and has been dropped.**
2. Removing those tools alone does NOT help — accuracy fell to 0.52, the confident wrong answers
   became 13 misses, `stiff_airframe` collapsed to 0.61 and the flip rate hit 0.89. Evidence
   values are load-bearing; they travelled through the same tools.
3. Separating them fixes it. `evidence_untimed` returns values and thresholds with no `t` and no
   ordering: accuracy 0.67 -> **0.96**, compass 0.00 -> **0.89**, cost **down 47%** because the
   agent stops chasing timing. Its reasoning becomes semantic — *"EKF_Magnetometer_Variance ...
   indicating an inconsistency in the compass data ... the direct cause"* — which is how B1 gets
   it right.

**It is NOT the ordering data itself.** `summarize()` reports advisories with `t_first`, so B1
sees the ordering too and scores 0.89 with zero symptom-as-root errors. The damage comes from
*querying and elaborating* the order, not from knowing it. `evidence_untimed` deliberately leaves
`summarize()` untouched, to keep that parity.

**Do not overclaim. Three limits, all load-bearing:**

* **B3 only TIES B1** (both 1.00 bundle-level) at 5,239 tokens against B1's 3,088. The agent is no
  longer worse; on this fault set it still buys nothing over one well-formed shot.
* **The fix is confounded with tool COUNT.** The winning surface is 2 tools where the losing one
  was 5. Removing timestamps and removing four tools happened in the same step, so the honest
  claim is *"this 5-tool surface was the defect and this 2-tool surface is not"* — **not**
  "timestamps specifically caused it". Isolating that needs a 5-tool surface with every
  timestamp stripped, holding count fixed. That run has not been done.
* **One model, one scenario.** Everything here is gemini-2.5-flash at temperature 0 / seed 0 on
  `compass_offset`. Write "this model on this fault", never "agents".

**Product implication:** Sentinel exists to tell an operator which alarm to act on. This says the
alarm log is the wrong thing to hand a diagnostic model — give it the evidence, not the order.

```bash
# the fix, reproducible
python scripts/e4_judge.py --bundles bundles --only compass_offset,stiff_airframe \
  --judges B0,B3 --withhold-tools list_advisories,ordering,detector_evidence,signal_window \
  --offer-tools evidence_untimed --provider gemini --out verdicts_untimed.json
```

Reproduce:

```bash
python scripts/e4_judge.py --bundles bundles --only compass_offset,stiff_airframe \
  --provider gemini --min-interval 1.0 --out verdicts_ambiguous.json
python scripts/e4_report.py --bundles bundles --verdicts verdicts_ambiguous.json \
  --only compass_offset,stiff_airframe --markdown
```

### Caveats that belong next to any published version

- **n is small.** `stiff_airframe` scores 1.00 for everyone and inflates all four headline
  numbers equally. Real n on the deciding scenario is 9.
- **This measures Gemini, not Claude.** `judges/llm.py` is written, tested and **still unrun** —
  no `ANTHROPIC_API_KEY`, and Anthropic-on-Vertex has zero quota (429 on every Claude model).
- **Clean controls were missing from this table.** `null` and `wind` were among the unloadable
  bundles, so "hallucinated 0/27" was untested. The re-fly (section 3) fixes this; re-run the
  sweep afterwards to get a table with controls in it.

## 2. Scenario library

| scenario | state |
|---|---|
| `null`, `vibration`, `gps_loss`, `wind` | base four, re-flown 2026-08-14 |
| **`compass_offset`** | **pair A, WORKS 3/3.** `ekf_inconsistency` 8.8 s -> `compass_inconsistency` 9.8 s, latency 0.7-0.8 s, 0 false positives, both mag axes verified at 400 |
| `hot_gains` | **pair C, BUILT AND NEVER FLOWN.** Next job |
| `stiff_airframe` | **RETIRED.** Kept in the file as the record |

**Pair A's mechanism is structural:** `compass.py:45 MIN_ANOMALY_S = 1.0` forces the compass
detector to wait a full second; `ekf.py` fires on threshold crossing. The 1.0 s gap is visible in
the flight data as exactly 1.0 s.

**Why `stiff_airframe` was retired.** Flown 3x at `SIM_ACC1_RND=90` and 3x at 70 —
`ambiguity_confirmed` false in all six, with `vibration_excessive` and `accel_clipping` landing in
the *same* 0.25 s cycle every time, so the gate broke the tie on `DETECTORS` order. It cannot be
fixed by sweeping, and 110 would not have helped: `accel_clipping` needs instantaneous peaks past
the ~16 g sensor range while `vibration_excessive` needs filtered VIBE (an RMS-like measure), and
`SIM_ACC1_RND` moves peak and RMS together by the same sigma. One parameter cannot separate them.

**Why the build plan's `gps_high_hdop` fallback was rejected — it cannot be built.**
`SIM_GPS_UBLOX.cpp:284` hardcodes `dop.hDOP = 121` (1.21), permanently below the detector's
`MAX_HDOP_THRESHOLD = 2.0`, and no `SIM_GPS1_*` parameter touches it. Confirmed in our own data:
`gps_high_hdop` is a declared symptom of `gps_loss` and has never once fired. Separately,
`gps.py` has no persistence gate at all, so it could not have provided pair A's mechanism anyway.

**Pair C (`hot_gains`) is BUILT but BLOCKED — and the block is SITL, not the config.**
Its ordering guarantee is real and stronger than pair A's (`WINDOW_SIZE_S=1.5` x
`MIN_SUSTAINED_WINDOWS=2` means `control_oscillation` cannot be advised for ~3 s). The ordering
half was even confirmed in flight: `actuator_saturation` led at +1.8 s. What fails is DETECTION —
`control_oscillation` never fires, so the root cause never arrives.

Six probe flights measured the tracking-error signal against the detector's own two criteria
(`max|desired-actual| >= 3.0 deg` and `>= 3.5 zero-crossings/s`). **The frequency criterion is
trivially met; the amplitude criterion never is.** Best of eight configurations: **2.44 deg**,
and non-monotonic in wind past ~20 m/s. Full table in the scenario's comment.

Two structural reasons, both worth knowing before anyone retries:
* raising gains makes the controller track *more* tightly, so tracking error shrinks while the
  ringing frequency rises — **gains are the frequency lever, not the amplitude one**;
* in guided flight `ATTITUDE_TARGET` leans into the wind to hold position, so desired follows
  actual and the error stays bounded however hard the air pushes.

3 deg of sustained tracking error is a real airframe with mechanical slop, not clean SITL physics.
**Do not fix this by lowering `OSCILLATION_AMPLITUDE_DEG`** — that makes the pair pass by
redefining the fault. The untried avenue is driving an oscillating *setpoint* via
`SET_ATTITUDE_TARGET` at ~2 Hz, which models pilot-induced oscillation and needs its own
justification.

**Confirmed useful side-finding:** SITL *does* deliver `ATTITUDE_TARGET` and
`NAV_CONTROLLER_OUTPUT` at 10 Hz, so the live adapter's desired-attitude path works and ATT
clears `MIN_ATT_RATE_HZ`. `detect_oscillation` can run in the realtime tier — it had never been
verified before.

### Three root causes SITL cannot produce

Worth stating plainly, because two build plans have now assumed otherwise:

| root cause | why unreachable |
|---|---|
| `gps_high_hdop` | `SIM_GPS_UBLOX.cpp:284` hardcodes hDOP = 1.21, below the 2.0 threshold |
| `control_oscillation` | tracking error bounded ~2.4 deg, below the 3.0 deg threshold |
| `vibration_excessive` *as an ambiguous pair* | `SIM_ACC1_RND` moves peak and RMS together |

**`compass_offset` remains the only working ambiguous pair.** The E4 result rests on it alone —
that is the single biggest weakness in the current evidence, and a second mechanism is the most
valuable thing anyone could add next.

## 3. Bundles — read this before trusting the archive

**13 bundles written earlier on 2026-08-14 fail to load**, and the cause is proven, not guessed:
they hash correctly under the *old* identity function. `_identity_payload` began excluding
`_TIMING_FIELDS` mid-session (correctly — replaying one log twice was producing two ids), but
`SCHEMA_VERSION` was not bumped alongside it, so stale bundles pass the version check and fail the
content hash.

Verified by recomputing three of them with the timing fields restored — `null_r1`, `gps_loss_r2`
and the replay bundle each reproduced their stored id exactly.

**RULE, now written into `bundle.py`: changing `_identity_payload` or `_TIMING_FIELDS` changes what
a `bundle_id` MEANS and must bump `SCHEMA_VERSION` in the same commit.** `SCHEMA_VERSION` was
deliberately *not* bumped retroactively — that would orphan the good bundles captured since.

The base four have been re-flown: **12/12 passed and the archive is now 24 loadable / 1 rejected.**

That re-fly also served as the refactor gate for everything changed today
(`--compare r8_results.json`), and it **PASSED** — claim-bearing fields unchanged against the
earlier baseline, wall-clock within tolerance. So the `read_param` drain, `--tune`/`--tag`,
`_leading()` and the `bundle.py` message all provably moved no measured number.

**`replay_2024-04-30 17-30-57.json` is the one remaining stale bundle** — it needs the original
`.BIN`, which is not anywhere in the repo. It is the evidence behind the 96.8% suppression claim,
so regenerate it before publishing that number.

## 4. Do this next

1. **Decide whether `evidence_untimed` becomes the default tool surface.** It is built, tested
   (99 tests) and proven, but deliberately opt-in via `OPTIONAL_SPECS` so the published table's
   five-tool configuration never changed underneath it. Promoting it to `SPECS` — and probably
   retiring `ordering` — is a product decision, not a code one: it changes what "the agent" means
   in every future run. Re-run the full B0/B1/B2/B3 sweep after deciding, so one table reports
   all four tiers under the same surface.
2. **Re-run the judge sweep with the base four included**, so the table finally has
   hallucination controls (`null`, `wind`) behind its "0 hallucinated" claim.
3. **Find a second ambiguous mechanism.** Only `compass_offset` works, so every E4 number rests
   on one scenario. `compass.py:45` and `oscillation.py`'s windows are the *only* two time gates
   in the detector set, and the second is unreachable — so a new pair likely needs a new gate,
   which means a detector change, not a scenario change.
4. Regenerate the replay bundle once the `.BIN` is located.

## 5. Environment — the things that will waste your time

```bash
# Use the analyzer venv. The system Python has no pytest and no deps.
../ardupilot-log-analyzer/.venv/Scripts/python.exe -m pytest -q     # 92 tests, all offline

# SITL lives in WSL. Binaries at /root/ardupilot/build/sitl/bin/
wsl -d Ubuntu-24.04 -- bash -c "ls /root/ardupilot/build/sitl/bin/"
```

- **LLM auth:** Gemini on Vertex works via ADC, project `gen-lang-client-0725459099`, location
  `global`. `ANTHROPIC_API_KEY` is not set and Anthropic-on-Vertex has zero quota.
- **Anything published from a Gemini run must name the model.**
- Python buffers stdout to a file; use `-u` on background SITL runs.
- Console output must stay ASCII — Windows cp1252 renders anything else as `?`.
- `pkill -f arducopter` from a `wsl bash -c` **kills its own shell** (self-match, exit 15). Match
  on the instance instead, as the harness does.

## 6. Decisions already made — do not re-litigate

| Decision | Why |
|---|---|
| Capture and judgment are separated | 4 min per flight vs ~96 judgments. Fly once, freeze a `RunBundle`, judge offline |
| `bundle_id` excludes wall-clock timings | It must fingerprint the flight, not the host |
| Transport failures degrade, never crash | Attributed to HARNESS, never to the model |
| Replay sets **no** ground-truth label | Deriving it from detector output grades the system against its own opinion |
| No RAG, no vector DB | There is no corpus |
| No LLM near flight control | Judges are offline; the bundle is a frozen file |
| `detector_evidence` is capped | Unbounded, it returned 191,465 chars (~48k tokens) for one call and degraded B3 on contact |
| Request pacing + retry on Gemini | Kept as insurance for the documented 429 history — it was **not** the cause of the degradations |

## 7. Fixed on 2026-08-14, all previously silent

Every one returned a confident wrong value instead of an error — the same failure mode this
product exists to detect.

| Defect | Effect | Fix |
|---|---|---|
| Gemini parallel tool-calls not merged | **16/27 B3 judgements degraded**, looked exactly like rate limiting | `gemini.py::_split` merges consecutive tool results, as `llm.py` already did |
| `read_param` did not drain | 3 bundles stamped `inject_verified: false` when injection had worked | drain queued `PARAM_VALUE` before requesting |
| `detector_evidence` unbounded | B3 could not complete a judgement | cap head/tail + `by_metric`, 191,465 -> 3,200 chars |
| `--only` loaded the whole archive | one stale bundle aborted every scoped sweep | filter filenames before loading |
| hash-mismatch message said "the file has been edited" | sent the reader hunting a tamper that never happened | names both causes |

New flags: `--provider {anthropic,gemini}`, `--only`, `--min-interval` (e4_judge);
`--tune SCEN:PARAM=VALUE`, `--tag` (r7_r8_scenarios); `--only` (e4_report).

## 8. Known-broken, with the actual reason

| Thing | Status |
|---|---|
| `coax` airframe | Not a SITL model — the physics layer has no coaxial model |
| `tilthvec` ("vtol") | Boots, no heartbeat |
| `dodeca-hexa` | Boots, will not arm |
| quadplane | Boots and arms, but **peak 0.0 m** — needs tilt-servo config |
| `judges/llm.py` (Anthropic) | Written, unit-tested, **never run live** — no key |
| `judges/grader.py`, kappa | Written, **never run** |
| `gps_high_hdop` | **Unreachable in SITL** — hDOP hardcoded at 1.21 |

## 9. Product surface, for reference

```bash
sentinel doctor                            # what is installed, what is missing, how to fix it
sentinel analyze FLIGHT.BIN --html r.html  # a log you already have -> findings + emailable report
sentinel replay FLIGHT.BIN                 # real flight through the realtime tier -> a bundle
sentinel watch --conn COM5,57600 --passive # live, without touching stream rates
```

`analyze` on a real log correctly flagged `BATT_LOW_VOLT = 10.5 V` on a 22.2 V pack — a 3S
failsafe left on a 6S battery, which would never have fired. Still the best demo in the repo.
