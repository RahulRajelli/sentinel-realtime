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
