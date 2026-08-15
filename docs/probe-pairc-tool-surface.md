# Probe: is the TOOL SURFACE the reason no judge beats B0 on pair C?

**Written and committed BEFORE the run**, like [`probe-pairc-e4.md`](./probe-pairc-e4.md) and
[`probe-hot-gains-damping.md`](./probe-hot-gains-damping.md).

## What prompted it

The pair C arms returned B3 = 0.00 on both `gpt-5.6-sol` and `gemini-3.7-flash`, with the symptom
named on **30 of 30** tool-agent judgements. Before concluding anything about judges, the obvious
question is whether the discriminating fact was reachable at all.

**It was, and it is already in the bundle.** Measured from `bundles/hot_gains_lowd_pairc_r0.json`:

| | `actuator_saturation` (symptom) | `control_oscillation` (cause) |
|---|---|---|
| first **incident** | 11.046 s | 11.140 s |
| first **advisory** | 11.062 s | 12.781 s |
| evidence | `Motor_PWM_Imbalance` 777 us vs threshold 300 | `Peak_Roll_Error_Amp` up to 53.5 deg vs threshold 3.0 |
| **exceedance** | **2.6x** | **13.8x - 17.8x** |

Two things fall out, and the second is the probe:

1. **Time cannot separate these two.** The incidents are **0.094 s** apart. The 1.719 s gap the
   scenario reports is the *advisory gate* (2 consecutive 1.5 s windows before `control_oscillation`
   may be raised), not physics. A judge reasoning from onset order is reasoning about a detector
   policy. This also means the ordering trap here is *not* the same shape as pair A's, where the
   physical gap is real and 1.0 s.
2. **Severity does separate them, by a factor of 5-7.** The cause exceeds its threshold ~17x; the
   symptom ~2.6x. That fact is in the recorded evidence today — but reaching it costs one
   `evidence_untimed` call per incident type plus a unit-free normalisation across `us` and `deg`
   done in the judge's head, against different thresholds.

**Hypothesis: the surface makes the comparison expensive, not impossible, and the judge does not
do it.**

## The change under test

One new tool, `exceedance_ranking()`. No arguments. For every incident type in the flight it
returns the metric with the largest `value / threshold` ratio, with the raw value, the threshold
and the unit. **No timestamps** — it stays inside the untimed default's design rationale rather
than smuggling ordering back in through a side door.

Added to `OPTIONAL_SPECS`, **not** to `SPECS`. The published 0.96 arm is untouched and stays
reproducible; this is offered explicitly with `--offer-tools exceedance_ranking`.

## Predictions

1. **B3 on pair C with the tool offered > 0.00.** The claim under test.
2. **B0 stays 0.00.** It does not call tools; if this moves, something is wrong with the harness.
3. B1 unchanged — it gets `summarize()` only and no tools, so it is a control on run-to-run drift.

## Falsifiers

* **If B3 stays 0.00**, the tool surface is not the bottleneck. That is a *stronger* negative than
  the one already published: the judge would have been handed the discriminating fact, pre-ranked,
  and still named the symptom. Report it as such.
* **THE CONTROL THAT DECIDES WHETHER THIS IS REAL — pair A must not regress.** `compass_offset`
  re-run with the same tool offered must stay at its published **0.96**. If pair C improves *and*
  pair A degrades, this is not a better surface. It is a fault-specific hack.

## The risk this probe is carrying, stated plainly

`exceedance_ranking` encodes a heuristic: **"the signal furthest past its threshold is the root
cause."** That is defensible diagnostics — ranking signals by how far past limit they sit is what
an engineer does in front of a log — and it reveals no ground truth, being derived entirely from
recorded evidence. It is also **one step away from handing over the answer**, because it happens to
be true of pair C.

The pair A control is the only thing separating "better instrumentation" from "fitted to this
flight". If pair A holds at 0.96 and pair C moves, the tool generalises across two mechanisms that
fail in different ways. If pair A drops, the honest report is that the tool is a hack, and it does
not enter the default surface — the same rule as
*never lower a detector threshold to make a scenario pass*, applied to the judge's side of the
harness.

**No threshold, prompt, or scenario is modified by this probe.** The only change is one additional
optional tool.

---

# RESULT — run 2026-08-15. Neither falsifier fired.

**The tool surface was the bottleneck.** `gpt-5.6-sol @ https://api.llmapi.ai/v1`, 5 runs per cell,
**0 degraded in all 20 runs**.

## Pair C — the claim under test

| run | 1 | 2 | 3 | 4 | 5 | mean |
|---|---|---|---|---|---|---|
| B3 **without** `exceedance_ranking` | 0.00 | 0.00 | 0.00 | 0.00 | 0.00 | **0.00** |
| B3 **with** it | 0.00 | 0.67 | 0.67 | 1.00 | 0.33 | **0.53** |

What B3 answered, 15 judgements per cell:

| | `control_oscillation` (cause) | `actuator_saturation` (symptom) |
|---|---|---|
| without | **0** | **15** |
| with | **8** | 7 |

**B0 = 0.00 in every run of both cells** (prediction 2 held — the cell stays readable). B1, which
receives no tools and exists here as a drift control, moved 0.13 -> 0.20, i.e. 2 of 15 correct to
3 of 15. That is inside run-to-run noise and is the expected null.

**This is the first time in this project that a judge has beaten B0 rather than tied it.**

## Pair A — the control that decides whether it is real

Re-run on the three published flights only (`compass_offset_r0/r1/r2`), same tool offered:

| | published | with `exceedance_ranking` | runs |
|---|---|---|---|
| B3 | 0.96 | **0.98** (1.00, 0.89, 1.00, 1.00, 1.00) | 5 |
| B1 | 0.71 | 0.74 (0.89, 0.67, 0.67, 0.67, 0.78) | 5 |
| B0 | 0.00 | 0.00 | 5 |

**No regression.** Both arms land marginally above their published values, well inside the 0.11
spread the variance study measured. So the tool is not trading pair A away to buy pair C.

## What this establishes

1. **The pair C failure was under-instrumentation, not incapacity.** The same model, same prompts,
   same route, same flight, goes from 0/15 to 8/15 on one additional read-only tool that states no
   conclusion and reveals no ground truth. The judge was never able to reach the discriminating
   fact at acceptable cost; it now can.
2. **It generalises across two mechanisms that fail differently.** Pair A's exceedances are *tied*
   (both incident types cite `EKF_Magnetometer_Variance` at 2.62x), so the tool is pure noise
   there — and pair A does not move. Pair C's differ by 5.6x, and pair C moves. A tool that helped
   only where it was decisive, and was harmless where it was not, is instrumentation rather than
   a fitted heuristic.
3. **It reframes the negative result published hours earlier.** "No judge beats the free baseline
   on pair C" was true, and the reason was the harness. That is a better finding than either a tie
   or a flat failure, and it is the E4 contribution: the agent's tool surface is a designed
   artifact that can be measured and is worth measuring.

## What it does NOT establish, and the honest ceiling

* **B3 is 0.53, not 0.9.** More than half the headroom is still unoccupied, and 7 of 15 judgements
  still name the symptom. This is a first result on this fault, not a solved fault.
* **The spread is 0.00 to 1.00 across five runs.** That is the widest of any arm measured in this
  repository. At **n = 1 flight** and 3 variants, a run can only take the values 0, 1/3, 2/3, 1.
  Quote the direction; do not quote 0.53 to two decimals as if it were stable.
* **One model.** `gemini-3.7-flash` scored 0.00 on pair C without the tool and has not been re-run
  with it. Until it is, "the tool surface was the bottleneck" is demonstrated for one judge.

---

## FOLLOW-UP, predicted before running: the same arm on `gemini-3.7-flash`

Identical cell to the gpt one — `--only hot_gains_lowd`, `v1,v2,v3`, `B0,B1,B3`,
`--offer-tools exceedance_ranking`, 5 runs, same route — with only the model changed.

`gemini-3.7-flash` is the sharpest available test of the claim, for the same reason it was chosen
as the control earlier: it is the **one model of nine** that never fell for the ordering trap on
pair A, and it still scored **0.00** on pair C with the default surface. If the bottleneck is the
surface rather than the judge, giving it the ranking should move it too.

**Prediction:** B3 > 0.00, and B0 stays 0.00.

**Falsifiers, both of which would matter:**

* **If it stays at 0.00**, then `exceedance_ranking` helps `gpt-5.6-sol` specifically, and
  "the tool surface was the bottleneck" is a claim about one judge, not about the harness. The
  headline gets narrowed to name the model it applies to.
* **If B0 moves off 0.00**, stop — the scenario has changed underneath the comparison and nothing
  in the cell is readable.

Either way this is the difference between "a model did better with a better tool" and "the tool
surface was the limiting factor", and only the second is a finding about agent design.
* **The promotion criterion is met but promotion has NOT been made.** The stated bar was "pair C
  improves AND pair A does not regress", and both held. `exceedance_ranking` nonetheless stays in
  `OPTIONAL_SPECS`, because moving it into the default changes what every published figure means
  and those figures must be re-run in the same commit that moves it. `test_tools_budget.py` asserts
  it is not in the default, so that decision cannot be taken by accident.
