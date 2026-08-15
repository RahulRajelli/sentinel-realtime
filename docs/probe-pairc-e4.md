# Probe: do the E4 arms beat B0 on pair C?

**Written and committed BEFORE the run**, for the same reason as
[`probe-hot-gains-damping.md`](./probe-hot-gains-damping.md): a prediction written after the
numbers arrive is not a prediction, and the falsifier is the part that is tempting to forget.

## The question

`hot_gains_lowd` (pair C) went into the fault set on 2026-08-15. B0, the free deterministic
baseline, scores **0.00** on it — it answers `actuator_saturation`, the symptom that fires 1.719 s
before the cause. On every fault captured before pair C, B0 scored **1.00**, because the root
cause's own detector happened to speak first.

That is why this run matters and could not have been done yesterday. Until now every comparison in
this repository was a tie at best: B0 was already perfect, so the ceiling for any judge was
"equally right, at cost". Pair C is the first fault with room above the baseline.

There is a second question riding along. **Every published accuracy in this repo was measured on
`compass_offset` and nothing else.** Pair C is a different mechanism — a control-loop limit cycle,
not a magnetometer bias — so it is the first opportunity to ask whether any of those numbers
describe the judges or just describe one fault.

## The arm, frozen

Exactly the published `gpt-5.6-sol untimed` arm (`results/crossmodel/gpt2_run*.json`), with one
variable moved: the scenario.

| | |
|---|---|
| bundles | `--only hot_gains_lowd` (**the published arm used `compass_offset`** — this is the one change) |
| variants | `v1,v2,v3` |
| judges | `B0,B1,B3` |
| withheld tools | none |
| offered optional tools | none |
| model + route | `gpt-5.6-sol @ https://api.llmapi.ai/v1` |
| runs | **5** (rule 2: never quote a single run) |

`gpt-5.6-sol` is chosen because it is the arm where B3 scored highest on pair A (0.96) — the
strongest existing result is the fairest thing to ask for generalisation. Rule 1: the route is
named because the same model scored 0.11 via ADC and 0.46 via this gateway.

7 verdicts per run (B0 once, B1 x3 variants, B3 x3 variants), 35 total. Dry run measured
~1020 tok/judgement, so this is a cheap run — the constraint here is honesty, not spend.

## Predictions

1. **B0 = 0.00 in all 5 runs.** Not really a prediction — a precondition. A non-zero B0 means the
   scenario was not ambiguous on that run and nothing else in the cell is readable.
2. **B1 > 0.00 and B3 > 0.00.** This is the actual claim under test: that pair C leaves room above
   the baseline for a judge to occupy.
3. **B3 >= B1**, replicating gpt's ordering on pair A (0.96 vs 0.71).

## Falsifiers

* **If B1 = B3 = 0.00**, pair C is not "a gap for E4 to close". It is a fault on which *every*
  judge names the symptom, and the claim now sitting in `FOCUS.md` and `HANDOFF.md` — that this
  run is what turns a tie into a result — is wrong and gets retracted like the other seven.
  Publish it either way; a negative result honestly measured is the stronger artifact.
* **If B3 < B1**, then gpt reproduces on a second mechanism the pattern previously seen only in
  gemini (tools making the judge *less* accurate), and "tools help the strong model" was a
  property of `compass_offset` rather than of the judge.
* **If B0 != 0.00 on any run**, stop and fix the scenario before reading anything else.

## The limitation, stated before the numbers

**n = 1 flight.** The published arms average over 3 bundles; this one has a single pair C capture,
so per-run accuracy can only take the values 0, 1/3, 2/3, 1. Rankings will be readable. Two-decimal
precision will not be, and no confidence interval computed over one bundle should be quoted.

The fix is **more flights, not more runs** — 2 more `hot_gains_lowd` repeats, minutes each in SITL.
Re-running the same bundle more times measures the judge's variance, not the fault's generality,
and those two have been confused in this project before.

---

# RESULT — run 2026-08-15. The falsifier fired.

**Prediction 2 is dead and prediction 3 is inverted.** No judge beat B0 on pair C. The tool agent
did not merely fail to beat it — it reproduced the deterministic baseline's exact answer on
**every single judgement**.

`gpt-5.6-sol @ https://api.llmapi.ai/v1`, 5 runs, 0 degraded:

| run | B0 | B1 | B3 |
|---|---|---|---|
| 1 | 0.00 | 0.00 | 0.00 |
| 2 | 0.00 | 0.33 | 0.00 |
| 3 | 0.00 | 0.00 | 0.00 |
| 4 | 0.00 | 0.00 | 0.00 |
| 5 | 0.00 | 0.33 | 0.00 |
| **mean** | **0.00** | **0.13** | **0.00** |

Prediction 1 held: B0 = 0.00 in all 5 runs, so the cell is readable.

## What they actually answered

| judge | `actuator_saturation` (the symptom) | `control_oscillation` (the cause) | no answer |
|---|---|---|---|
| B0 | 5 / 5 | 0 | 0 |
| B1 | 7 / 15 | **2** | 6 |
| B3 | **15 / 15** | **0** | 0 |

**B3 named the symptom on 15 of 15 judgements.** Giving the judge tools did not move it toward the
cause; it moved it to answering exactly like the free rule, every time, at 2,401 tokens per
judgement against B1's 778. Cost per correct answer for B3 on this fault is undefined — there are
no correct answers to divide by.

## Is it the judge or the fault? — the disambiguation run

A single model failing proves little, so the arm was repeated on **`gemini-3.7-flash`**, the one
model in the 9-model prevalence sweep that **never** fell for the ordering trap on pair A
(`sym 0.00/9`, `acc 1.00` — the best row in that table). Same route, same arm, 5 runs, 0 degraded:

| judge | mean | `actuator_saturation` | `control_oscillation` |
|---|---|---|---|
| B0 | 0.00 | 5 / 5 | 0 |
| B1 | 0.07 | 14 / 15 | 1 |
| B3 | **0.00** | **15 / 15** | 0 |

**The model that never falls for the trap falls for it on 100% of tool-agent judgements here.**

Across both models: **30 of 30 B3 judgements named the symptom. Zero exceptions.**

## What this settles, and what it costs

1. **The 0.96 was a property of `compass_offset`, not of the judge.** Every published accuracy in
   this repository was measured on one fault. Moved to a second mechanism, gpt's B3 goes 0.96 ->
   0.00 and gemini-3.7-flash's 1.00 -> 0.00. This is retraction 8.
2. **The ordering trap is not (only) a model weakness.** On pair A it was graded — 4 of 9 models
   fell for it, 4 never did, and capability tracked resistance within families. On pair C it is
   universal across the two models tried, including the most resistant one. Something about this
   fault, or about what the tool surface exposes of it, defeats all of them.
3. **Tools made it strictly worse.** B1, with no tools, is the only arm that was ever right
   (3 of 30 judgements across both models). B3, with tools, was never right, at 3-4x the tokens.
   The prompt-variant finding from session 2 pointed this way; this is the sharp version.
4. **The honest headline is a negative one.** "Pair C gives the agent a gap to close" was correct
   about the gap and wrong about the closing: B0 = 0.00 leaves room, and nothing measured so far
   occupies it. Per the project's own rule, that gets published rather than buried.

## What this does NOT establish

* **n = 1 flight**, as stated before the run. This is one pair C capture. Two more repeats are
  cheap and should be flown before any of the above is written into the whitepaper as a rate.
* **Two models, not nine.** The prevalence sweep should be re-run on pair C to see whether the
  4-of-9 split collapses to 0-of-9, which is what these two suggest but do not prove.
* **It is not established that the fault is unsolvable.** The most likely mechanism is that the
  tool surface reports `actuator_saturation` first and nothing the judge can call distinguishes a
  saturating actuator that is *causing* a limit cycle from one *responding* to it. That is a
  hypothesis, and it is testable: pair C is severe (53.5 deg), so amplitude and zero-crossing rate
  are both visible in the data the tools already return. If a tool that exposes them fixes this,
  the finding becomes "the tool surface was wrong", not "agents cannot do this".
