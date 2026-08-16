# Safe-action mapping — specification

**Status: design, nothing built.** Written 2026-08-16, session 5, against the question session 4
could not answer: *can such a drone actually be saved, or is this posthumous log reading?*

Read section 0 first. Everything after it is detail, and section 11 is the part that decides
whether this is worth building.

---

## 0. The claim, and the claim it is NOT

> **For survival you do not need the root cause. You need the action that is correct under
> ambiguity.**

Both members of an ambiguous pair — the symptom that fires first and the cause that fires later —
imply the *same* restriction on what the aircraft may do next. That restriction is decidable at the
SYMPTOM, deterministically, with **no model in the loop**, seconds before any judge could name the
cause.

This is not the E4 claim and it does not replace it. E4 asks *which fault is it* — a question whose
answer decides which part gets swapped, which gain gets changed, and whether the aircraft flies
tomorrow. That is a **hangar** question and it is worth answering. **Safe action is the air
question**, and conflating the two is the gap in this project's product story: every measurement in
this repo is about naming the cause, and naming the cause is not what keeps an airframe intact.

Three things follow immediately, and two of them are uncomfortable:

1. On both ambiguous pairs currently in the fault library, the safe action derived from the
   symptom alone is **identical or a superset** of the one the true cause would demand (section 5).
   So for survival on these two faults, the E4 agent layer buys nothing.
2. The measurable gain is **not latency**. It is 0.95 s on pair A and 1.72 s on pair C — real, and
   small next to an operator's comprehension time. The gain is that the operator is **not told the
   wrong thing** for those seconds (section 6.2).
3. Sentinel cannot execute any of this. `ONBOARD-THREAT-MODEL.md` rule 1 forbids it from commanding
   the aircraft at all. This layer emits a *recommendation*; section 7 is about who acts on it and
   what would have to change for anything else.

---

## 1. Scope

**In scope.** A total, deterministic function from the set of currently-active advisories to a set
of authority withdrawals, its correctness condition, its evaluation, and the tests that gate it.

**Out of scope, deliberately.** Any command channel to the aircraft. Any model. Any operator UI
(`FOCUS.md` lists E5 under *Deliberately NOT building*). Any change to a detector threshold —
rule 5 of section 7 in `HANDOFF.md` applies here as everywhere: never move a threshold to make a
scenario behave.

---

## 2. The action vocabulary

Two tiers, and the split is load-bearing. Everything in section 4 depends on it.

### Tier 1 — withdrawals. Subtractive, and they compose.

Each removes an authority or a dependency the aircraft was using. Taking two is never worse than
taking one, because each strictly shrinks what the aircraft is permitted to do.

| code | withdraws | the aircraft may no longer |
|---|---|---|
| `NAV_UNTRUSTED` | position and heading as inputs to control | fly any position- or heading-dependent mode: AUTO, GUIDED, LOITER, **RTL** |
| `ALT_UNTRUSTED` | the inertial/baro vertical estimate | rely on altitude hold or automatic descent rate |
| `ATT_AUTHORITY_LIMIT` | aggressive attitude commanding | demand large lean angles or fast attitude changes |

**`NAV_UNTRUSTED` forbids RTL.** This is the single most useful line in the document. RTL is the
reflex response to almost any in-flight advisory, and it is powered by exactly the estimate that
`ekf_inconsistency`, `compass_inconsistency` and `gps_fix_loss` are reporting as broken. The
convergent safe action for the entire navigation family is *stop using navigation*, and the
convergent wrong action is *press RTL*.

### Tier 2 — dispositions. NOT subtractive. They do not compose freely.

| code | means | precondition Sentinel cannot evaluate |
|---|---|---|
| `TERMINATE_SOON` | end the flight at the nearest safe point | that a safe descent point exists and is reachable |

Landing is an action with its own risk. Landing over water, over a crowd, on a slope, or into a
fence is worse than the fault that prompted it. **Sentinel has no map, no fence state, no terrain,
and no view of what is under the aircraft**, so it cannot decide that the precondition holds. Tier 2
is therefore emitted as a recommendation carrying its precondition in the text, never as a
conclusion, and the correctness theorem in section 4 is stated over Tier 1 only.

Splitting the tiers is what keeps the theorem honest. Folding `TERMINATE_SOON` in with the
withdrawals would have made the proof look stronger and the design worse.

---

## 3. The mapping

Total over the live advisory vocabulary. The live vocabulary is exactly ten types, from the seven
detectors registered in `runner.py:39` — `timeline` and `errors` are offline-only, so `error_*` and
`mode_entry_*` cannot appear here and are excluded by construction rather than by omission.

| advisory | Tier 1 | Tier 2 | why |
|---|---|---|---|
| `ekf_inconsistency` | `NAV_UNTRUSTED`, `ALT_UNTRUSTED` | `TERMINATE_SOON` | the state estimate is the thing in doubt, and it feeds both horizontal and vertical control |
| `compass_inconsistency` | `NAV_UNTRUSTED` | `TERMINATE_SOON` | heading is unreliable; the vertical channel is not implicated |
| `gps_fix_loss` | `NAV_UNTRUSTED` | `TERMINATE_SOON` | position is unavailable, not merely degraded |
| `gps_high_hdop` | `NAV_UNTRUSTED` | — | position is degraded. Degradation alone does not justify ending a flight |
| `vibration_excessive` | `ALT_UNTRUSTED`, `ATT_AUTHORITY_LIMIT` | `TERMINATE_SOON` | vibration corrupts the vertical velocity estimate first, and drives control noise second |
| `accel_clipping` | `ALT_UNTRUSTED`, `ATT_AUTHORITY_LIMIT` | `TERMINATE_SOON` | a saturated accelerometer is not measuring anything; the estimate downstream of it is fiction |
| `actuator_saturation` | `ATT_AUTHORITY_LIMIT`, `NAV_UNTRUSTED` | `TERMINATE_SOON` | there is no control margin left. Nav commands consume margin, so nav goes too |
| `control_oscillation` | `ATT_AUTHORITY_LIMIT`, `NAV_UNTRUSTED` | `TERMINATE_SOON` | the attitude loop is unstable, and position control drives the attitude loop |
| `battery_voltage_sag` | — | `TERMINATE_SOON` | endurance is the thing that is gone. No authority is untrustworthy |
| `battery_threshold_misconfigured` | — | — | **a pre-flight configuration finding.** Mapping it to any flight action would be the exact over-trigger failure this spec exists to punish |

Two rows are worth defending explicitly.

**`battery_threshold_misconfigured` maps to nothing.** It is a statement about the aircraft's
parameters, not about its state. A mapping that reflexively lands on every advisory is the
degenerate baseline of section 9.3, and this is the first row where refusing to act is the correct
design.

**`gps_high_hdop` is unreachable in SITL** — `SIM_GPS_UBLOX.cpp:284` hardcodes hDOP 1.21, below the
2.0 threshold, and no `SIM_GPS1_*` parameter touches it (`HANDOFF.md` section 8). Its row is
therefore reasoned, not measured, and must stay flagged as such in any published table.

### 3.1 Severity is recorded and NOT used

The gate carries `severity` and `peak_severity` on every advisory, and v1 ignores both.

Severity is computed per detector against unrelated scales: `ekf.py` calls it critical at variance
5.0, `oscillation.py` at 8.0 degrees, `vibration.py` at 60 m/s^2, `actuator.py` at two saturated
motors. A `warning` from one and a `warning` from another are not the same quantity, and keying a
safety decision on a comparison between them would import an incomparable number into the one place
it must not appear. Pair A raised both of its advisories at `warning` and pair C raised both at
`critical`, so on the current library severity carries no discriminating information anyway.

If severity is ever admitted, it must be admitted as a per-type rule with its own justification, not
as a cross-detector ordering.

---

## 4. Composition, and the correctness condition

**The rule.** The emitted action set is the **union** of the mapping over every *currently active*
advisory:

```
emitted(t) = union of MAP[a.type] for a in gate.active
```

Union, not the mapping of the first advisory and not the mapping of the most severe one. Union is
monotone: adding an advisory can only remove more authority and can never restore any. That is the
whole reason this is decidable without knowing which advisory is the cause.

**The correctness condition, for an ambiguous pair (S = symptom, C = cause):**

```
Tier1(S) is a superset of Tier1(C)
```

If that holds, then acting at symptom time is never a downgrade relative to knowing the truth: every
withdrawal the true cause would have demanded is already in force before the cause is nameable. The
excess — `Tier1(S) \ Tier1(C)` — is the over-restriction cost, and it is measured, not waved at.

**Two consumer details that the existing code gets wrong for this purpose:**

* **Read `gate.active`, not the return of `gate.submit()`.** `submit` returns advisories *raised
  this cycle*; a fault that is suppressed by cooldown is still true. The union must be over what
  currently holds.
* **Withdrawals must latch; the gate's advisories do not.** `EscalationGate.__init__` defaults
  `clear_after_s = 10.0`, so an advisory absent for 10 s is dropped from `active`. Under the union
  rule that silently *restores* authority — an intermittent fault would hand back RTL between
  bursts. Pair C's own bundle shows the pattern: `actuator_saturation` at 11.062 s, then again at
  31.062 s, twenty seconds apart in one flight. **Once withdrawn, an authority stays withdrawn for
  the remainder of the flight**, unless an operator clears it explicitly. Restoration is not
  Sentinel's decision to make.

---

## 5. Proof obligations, discharged against the current library

### Pair A — `compass_offset` (`bundles/compass_offset_r0.json`)

```
t =  8.062  inject
t =  9.062  ekf_inconsistency        SYMPTOM  warning   (+1.000)
t = 10.016  compass_inconsistency    CAUSE    warning   (+1.954)     gap 0.954 s
```

| | Tier 1 | Tier 2 |
|---|---|---|
| S = `ekf_inconsistency` | `NAV_UNTRUSTED`, `ALT_UNTRUSTED` | `TERMINATE_SOON` |
| C = `compass_inconsistency` | `NAV_UNTRUSTED` | `TERMINATE_SOON` |

`Tier1(S)` is a strict superset of `Tier1(C)`. **Condition holds, with one dimension of excess**
(`ALT_UNTRUSTED`). The cost of that excess: an operator flies the last thirty seconds in STABILIZE
rather than ALT_HOLD, managing throttle by hand. Conservative, and cheap.

Measured across the four compass flights the symptom-to-cause gap is 0.95–1.00 s.

### Pair C — `hot_gains_lowd` (`bundles/hot_gains_lowd_pairc_r0.json`)

```
t =  8.000  inject
t = 11.062  actuator_saturation      SYMPTOM  critical  (+3.062)
t = 12.781  control_oscillation      CAUSE    critical  (+4.781)     gap 1.719 s
```

| | Tier 1 | Tier 2 |
|---|---|---|
| S = `actuator_saturation` | `ATT_AUTHORITY_LIMIT`, `NAV_UNTRUSTED` | `TERMINATE_SOON` |
| C = `control_oscillation` | `ATT_AUTHORITY_LIMIT`, `NAV_UNTRUSTED` | `TERMINATE_SOON` |

**Equal. Zero excess.** Pair C is exactly decidable at the symptom. The action is fully determined
1.719 s before the cause is nameable, and no judge — deterministic or otherwise — is needed to
determine it.

This is the sharpest result in the document and it should be stated with its consequence attached:
**on pair C, the entire E4 apparatus is irrelevant to survival.** What E4 answers is whether the
maintainer detunes the controller or inspects the ESCs after landing, which is a real question that
this layer does not touch.

### Recoverability — the reason any of this matters

`HANDOFF.md` section 1 records it already: restoring the gains stabilised the aircraft, which held
altitude for 30 s at 26–33 degrees of error and landed and disarmed cleanly on every run. The fault
is survivable. The intervention exists. Nothing in this repo currently connects the two, and that
is what this spec is for.

---

## 6. What "reduce authority" means concretely

### 6.1 Per withdrawal

| withdrawal | what the operator does | what it explicitly does NOT mean |
|---|---|---|
| `NAV_UNTRUSTED` | switch to ALT_HOLD (STABILIZE if `ALT_UNTRUSTED` is also in force); fly home on visual reference; **do not press RTL, AUTO or LOITER** | it does not mean the aircraft is lost, and it does not mean land immediately |
| `ALT_UNTRUSTED` | expect altitude excursions; take manual throttle; descend on visual reference | it does not mean the aircraft is falling |
| `ATT_AUTHORITY_LIMIT` | reduce stick deflection; no aggressive manoeuvres; gentle descent; **do not attempt a fast RTL** | **it does not mean shut down a motor**, and it does not mean increase gains to "get control back" |
| `TERMINATE_SOON` | land at the nearest safe point *if one exists and is reachable* — the operator's judgement, not Sentinel's | it does not mean descend where you are |

### 6.2 The wrong actions this exists to prevent

This is the measurable product claim, and it is about correctness rather than speed.

| advisory | what the current system tells an operator | what is actually true | cost of the wrong action |
|---|---|---|---|
| `actuator_saturation` | `actuator.py:160` prints *"Check motor/ESC health, propeller damage, CG offset, or mechanical twist in the vehicle frame."* | the attitude loop is oscillating and driving the outputs to their limits | on the ground: a wrongly-swapped ESC. **In the air, "motor failure" on a quad can mean shutting a motor down, which is unrecoverable.** |
| `ekf_inconsistency` | a navigation-filter warning, which reads as *get it home automatically* | heading or position is the broken input | RTL is powered by the broken estimate. The reflex response uses the failed subsystem |

**`actuator.py`'s note text is a hangar instruction printed during flight.** That is a concrete
defect, not a framing complaint, and fixing it is work item W4 in section 12. The detector's
diagnostic notes and the in-flight action text are different audiences and must be different
fields.

---

## 7. Who executes it

`ONBOARD-THREAT-MODEL.md` section 0 lists four rules that cannot be traded. Three deployment shapes,
scored against them:

| shape | rule 1 (read-only MAVLink) | rule 2 (no API response influences flight) | verdict |
|---|---|---|---|
| **A. Ground-station advisory.** The mapping runs in the GCS process next to `sentinel watch` and prints action text to the operator. | untouched — nothing is written to the aircraft | untouched — no API is involved. The mapping is local deterministic code, not a remote answer | **permitted today.** This is the v1 target |
| **B. Onboard advisory to a pilot in flight.** Runs on the companion, displayed to the pilot. | untouched | rule 2 forbids *"advisory text shown to a pilot mid-flight"* from a **remote endpoint**. This text has no remote input. The letter permits it; the spirit deserves a review before it ships | **conditional.** Needs an explicit finding recorded, not an inference from this table |
| **C. Autonomous execution.** Sentinel changes mode or limits demand itself. | **violates rule 1.** | — | **forbidden.** Would require a separately-reviewed relaxation, and this spec recommends against it: an authority-withdrawal path is a remote control surface the moment the companion is compromised, which is the exact scenario section 0 of the threat model is built around |

### 7.1 A correctness finding that this spec depends on

`ONBOARD-THREAT-MODEL.md` section 3 item 2 says read-only MAVLink is *"enforced by a test, the way
the no-LLM-in-control rule is today."* Two problems, both found while writing this:

1. `cmd_watch` calls `runner.request_streams()` and `runner.fetch_params()`, which **write** to the
   autopilot. Only `--passive` is genuinely read-only. Already recorded in `HANDOFF.md` section 1.
2. **The no-LLM-in-control test does not exist.** No test in `tests/` inspects the imports of
   `runner.py`, `capture.py` or `gate.py`. The *property* is true — neither module imports anything
   from `sentinel.judges` — but the claim that a test enforces it is false, and it is quoted in a
   security document as though it were enforced.

Both must be reconciled before shape A ships, because this layer's entire safety argument is "no
model in the loop, and it cannot command the aircraft". An unenforced rule is a story about how
things used to work (`METHOD.md`, step 3).

---

## 8. Latency budget

Measured against the moment the fault begins, for shape A.

| term | pair A | pair C | measured? |
|---|---|---|---|
| onset to symptom advisory | 1.000 s | 3.062 s | yes, n=4 and n=1 |
| gate cadence quantisation | <= 0.25 s | <= 0.25 s | yes (ambiguous scenarios run 0.25 s; the default is 1.0 s) |
| detector pass | p95 14.4 ms, peak 114.6 ms against a 1000 ms budget | same | yes, 2,973 cycles |
| gate submit | O(active advisories) | same | negligible |
| **link delay, GCS to operator** | **unmeasured** | **unmeasured** | **no** |
| operator comprehension and reaction | **unmeasured, and larger than every term above** | same | **no** |

**Floor, by construction.** `oscillation.py` needs `WINDOW_SIZE_S = 1.5` x
`MIN_SUSTAINED_WINDOWS = 2`, so **no action keyed to `control_oscillation` can be faster than 3.0 s**
after the amplitude first crosses. The mapping does not care: it fires on `actuator_saturation`,
which on pair C arrives at +3.062 s. The 3.0 s gate is the cause detector's latency, not the action's.

**The link is the blocking dependency and it is not a nice-to-have.** The full MAVLink stream is
164% of a 57600 SiK radio (`MAVLINK-EFFICIENCY.md`); 56.1% of streamed bytes are discarded. At an
offered load above capacity, queuing delay is unbounded, and an unbounded term dominates a 1.7 s
budget completely. **A safe-action story that only works on a link the common radio cannot carry is
not a story.** Selective stream rates take this to 72% and are a prerequisite, not a follow-up.

**The honest reading of this table:** the latency saving is 0.95–1.72 s, and the two unmeasured
terms are both plausibly larger than that. Do not sell this on speed. Sell it on section 6.2 —
the operator is not told to check the ESCs while the controller is oscillating.

---

## 9. Evaluation

A safe-action mapping that is never wrong but always says "land" is useless. The metric must punish
over-triggering, and it must be a golden set like the existing scorer rather than a judgement call.

### 9.1 Ground truth lives in the bundle

Following `score.py`'s doctrine — *"Ground truth lives in the bundle, not here, so a bundle handed
to someone else is self-contained and independently scoreable"* — three new fields:

```json
"safe_action_required":  ["NAV_UNTRUSTED", "ALT_UNTRUSTED"],
"safe_action_forbidden": ["RTL", "AUTO", "MOTOR_SHUTDOWN", "GAIN_INCREASE"],
"safe_action_deadline_s": 2.0
```

`required` is the **minimal** Tier 1 set justified by the injected fault. `forbidden` names actions
that are harmful given that fault, drawn from the wrong-action column of section 6.2. `deadline_s`
is measured from `t_inject`.

**Adding these fields requires no `SCHEMA_VERSION` bump, and this was checked rather than assumed.**
`_identity_payload` (`bundle.py:202`) enumerates its fields explicitly — `schema_version`,
`scenario`, `expected_root_cause`, `injection`, `t_inject`, `params_hash`, `cycles`, `advisories`.
Anything not on that list is excluded, so three new optional fields with defaults leave every stored
`bundle_id` unchanged and every existing bundle still loads and still hashes to its stored id.
`airframe_id` (`bundle.py:141-147`) is the in-code precedent for exactly this move, and records the
same reasoning.

This is the one place where being wrong costs a day — it did on 2026-08-15 — so the migration must
still be preceded by a re-read of the section 9 warning in `HANDOFF.md`. **If any of these fields
ever moves into `_identity_payload`, the version bump, the migrator and `resolvable_identities()`
are all owed in the same commit.**

### 9.2 Four numbers, never summed into one

Per bundle, aggregated at the bundle level (`ci_bundle`, per the standing rule — never per
judgement, and there are no judgements here anyway):

| metric | definition | direction |
|---|---|---|
| **SAFETY** | boolean: the full `required` set was emitted by `deadline_s`, **and** no `forbidden` action was ever implied | pass rate. Any failure is absolute |
| **EXCESS** | count of Tier 1 codes emitted at `t_end` that are not in `required` | lower is better. This is the over-restriction cost |
| **FALSE_TERMINATE** | boolean: `TERMINATE_SOON` emitted on a bundle whose `expected_root_cause` is `null` | must be 0 |
| **ACTION_LATENCY** | t of first complete `required` emission, minus `t_inject`; reported beside the root-cause advisory time | informational, not a pass gate |

Summing these into one score is how "always land" hides. They are reported as a tuple or not at all.

### 9.3 Two degenerate baselines, both of which must be beaten

Directly from `METHOD.md`: write down the free heuristic in one sentence, then prove your method
beats it. There are two here, in opposite directions, and the bar is stated **before** anything is
measured.

| baseline | one sentence | expected behaviour |
|---|---|---|
| `A_MAX` | *"If anything at all is wrong, withdraw everything and land."* | SAFETY = 1.00 by construction. EXCESS maximal. FALSE_TERMINATE on any benign bundle that raises an advisory |
| `A_NULL` | *"Never restrict anything."* | EXCESS = 0 and FALSE_TERMINATE = 0 by construction. SAFETY = 0.00 on every fault bundle |

**The bar: the mapping must tie `A_MAX` on SAFETY and tie `A_NULL` on FALSE_TERMINATE and EXCESS on
benign bundles.** Falling short on either side is a failure, and the two-sidedness is what makes the
result informative rather than assumed.

### 9.4 What this evaluation CANNOT measure today, and why that is the finding

**No benign bundle in the current library raises a single advisory.** `null`: 0 incidents, 0
advisories. `wind` at 18 m/s: 0 incidents, 0 advisories, across all three re-flown reps.

Therefore `A_MAX` — defined realistically as *"emit everything whenever any advisory is active"* —
emits nothing on every benign bundle in the library, and is **indistinguishable from the real
mapping** on exactly the axis the mapping exists to win. FALSE_TERMINATE is 0 for both. EXCESS on
benign bundles is 0 for both.

**The over-trigger metric is defined and has n = 0.** That is not a reason to weaken it. It is a
capture job, and it is the highest-value one this spec creates:

| candidate | hypothesis | predicted mechanism |
|---|---|---|
| `heavy_climb` | high-collective climb at payload raises `actuator_saturation` with no fault | outputs approach `MOT_SPIN_MAX` at high throttle; `actuator.py` keys on the upper limit only |
| `gust_25` | wind at 25–30 m/s with **default** gains raises something | 18 m/s raised nothing; the threshold, if any, is above that |
| `fast_yaw` | a rapid commanded yaw raises a transient `ekf_inconsistency` | `ekf.py` `WARNING_THRESHOLD = 1.0` fires on crossing with no persistence gate — the same property that makes pair A ambiguous |

**Falsifier, stated in advance:** if none of these — or anything else — can be made to raise an
advisory on a healthy aircraft, then over-triggering is genuinely not a risk in this detector set,
and **that is the result**, recorded as a measured negative rather than an unexamined assumption.
Per `METHOD.md`: *record what you could not produce.*

---

## 10. Implementation shape

`sentinel/safe_action.py`, consuming `gate.active`. Roughly 80 lines: a frozen `dict[str,
frozenset[str]]`, a union function, a latch, and the render.

Five tests, and each exists because skipping it produces a specific failure:

| test | asserts | the failure it prevents |
|---|---|---|
| `test_mapping_is_total` | every type the seven live detectors can emit has a row | a new detector ships and its advisory silently maps to nothing |
| `test_symptom_dominates_cause_on_every_ambiguous_pair` | for every r8 row with `ambiguous: true`, `Tier1(first_advised)` is a superset of `Tier1(expected)` | the section 5 argument decays into a story about two flights from August |
| `test_withdrawals_latch` | an authority withdrawn stays withdrawn past `clear_after_s` | the gate hands back RTL between bursts of an intermittent fault |
| `test_beats_both_degenerate_baselines` | ties `A_MAX` on SAFETY, ties `A_NULL` on FALSE_TERMINATE | a mapping that always lands passes review |
| `test_no_model_client_in_the_safety_path` | `safe_action.py`, `gate.py`, `runner.py` and `capture.py` import nothing from `sentinel.judges` | the rule quoted in `ONBOARD-THREAT-MODEL.md` section 0 stays a claim rather than a test — **which is its current state** (section 7.1) |

The second test is the one that matters. It is the section 5 proof, executed on every commit,
against whatever ambiguous pairs exist at that moment rather than the two that exist today.

---

## 11. Limits and falsifiers

Stated before the thing is built, per the repo's own discipline.

1. **n = 2 pairs.** The condition `Tier1(S) ⊇ Tier1(C)` is verified on the only two ambiguous pairs
   that exist. Two is not a distribution. Pair B is retired and pair D does not exist.
2. **The condition is falsifiable, and here is what would falsify it.** A pair where the symptom
   implies a withdrawal the cause needs *revoked* — a fault whose correct response is to *use*
   navigation to escape (drifting over water, toward a fence, into airspace) while the leading
   symptom says navigation is untrustworthy. The current vocabulary cannot express "use NAV", which
   is precisely why every action here is subtractive; the day a fault needs an additive response,
   this design does not cover it and must say so rather than stretch.
3. **Tier 2 is the weak joint.** `TERMINATE_SOON` does not compose safely and its precondition is
   unevaluable by Sentinel. Every argument in section 4 is about Tier 1. If a reviewer reads the
   theorem as covering landing, the document has failed.
4. **Severity is discarded** (section 3.1). Defensible today because both pairs are internally
   uniform; a pair that is `warning`/`critical` across its two members would make this a real
   question.
5. **The over-trigger metric has no data** (section 9.4).
6. **The link delay is unmeasured** (section 8) and could dominate the entire budget.
7. **Pair C is severe.** A +/-40 degree limit cycle alarms any operator without help; pair A's
   compass offset is invisible until the advisory. The mapping's value is much higher on faults an
   operator cannot see, and only one of the two is that.
8. **Nothing here is validated against a pilot.** Every claim about what an operator would do wrong
   is reasoned from the advisory text, not observed. That is a genuine gap and it is the kind that
   a single conversation with a commercial operator would close.

---

## 12. Work items this creates

| # | item | depends on | why |
|---|---|---|---|
| W1 | Capture at least one benign-but-alarming flight (section 9.4) | SITL, minutes | without it the over-trigger metric cannot discriminate, and the whole evaluation is one-sided |
| W2 | Selective MAVLink stream rates, 164% -> 72% | `MAVLINK-EFFICIENCY.md` | an unbounded queuing delay dominates a 1.7 s budget. **Prerequisite, not a follow-up** |
| W3 | Reconcile the read-only claim and **write the no-LLM-in-control test that section 0 of the threat model already claims exists** | — | the safety argument for this layer is exactly those two rules |
| W4 | Split `actuator.py`'s diagnostic notes from in-flight action text | — | it currently prints a hangar instruction mid-flight (section 6.2) |
| W5 | Implement `sentinel/safe_action.py` and its five tests | W1 for the full evaluation; nothing for the mapping itself | — |
| W6 | Record a finding on deployment shape B (section 7) | — | do not let an inference from a table become the precedent for showing text to a pilot in flight |

---

*Companion documents: [METHOD.md](METHOD.md) for the baseline discipline this evaluation borrows,
[ONBOARD-THREAT-MODEL.md](ONBOARD-THREAT-MODEL.md) for the four rules that bound section 7,
[MAVLINK-EFFICIENCY.md](MAVLINK-EFFICIENCY.md) for the link budget in section 8.*
