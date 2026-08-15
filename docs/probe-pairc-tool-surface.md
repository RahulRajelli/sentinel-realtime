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
