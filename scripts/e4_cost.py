#!/usr/bin/env python3
"""What each judge COST, and what this design could ever have detected.

    python -u scripts/e4_cost.py
    python -u scripts/e4_cost.py --bundles bundles --only compass_offset

Four things the write-ups assert or imply but never quantify. All of it is computed from
verdict files already in the repo -- this spends nothing and needs no key.

1. COST PER CORRECT ANSWER. The whole argument is "an expensive method must beat a free rule at
   equal cost", and B2 exists solely to be budget-matched to B3. Cost appeared nowhere. Accuracy
   alone cannot answer "was it worth it"; tokens-per-correct-answer can.

2. CROSS-MODEL EFFICIENCY on an identical arm. Same bundles, same prompts, same tools -- so any
   difference in spend is the model, not the task.

3. WHY THE MISSES HAPPENED. `attribute()` already classifies every zero as model / harness /
   environment. A table of accuracies with no attribution column invites the reader to assume
   every miss was the model reasoning badly, and some of them are transport failures.

4. WHAT THIS DESIGN COULD DETECT. The retracted significance claim was not a slip of arithmetic;
   it was structural. This computes the best p-value the design can produce at a given n --
   see the note in `detectability()`, which is the most important output here.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_FLIGHTDX_SRC = _ROOT.parent / "ardupilot-log-analyzer" / "src"
if _FLIGHTDX_SRC.exists():
    sys.path.insert(1, str(_FLIGHTDX_SRC))

from sentinel.bundle import load_all      # noqa: E402
from sentinel.judges import Verdict       # noqa: E402
from sentinel.score import score_all      # noqa: E402

ARMS = [
    ("gpt-5.6-sol   untimed", "results/crossmodel/gpt2_run*.json"),
    ("gpt-5.6-sol   timed",   "results/crossmodel/gpt_timed*.json"),
    ("gemini-2.5-fl untimed", "results/isolation/iso_untimed*.json"),
    ("gemini-2.5-fl timed",   "results/isolation/iso_timed*.json"),
    ("gemini-2.5-fl variance", "variance/var_run*.json"),
]


def _load(path: Path):
    raw = json.loads(path.read_text())
    vs = raw if isinstance(raw, list) else raw.get("verdicts", [])
    return [Verdict.model_validate(v) for v in vs], vs


def collect(bundles, pattern: str):
    """Per-judge totals for one arm."""
    agg = collections.defaultdict(lambda: {
        "tin": 0, "tout": 0, "calls": 0, "wall": 0.0, "n": 0,
        "acc_sum": 0.0, "runs": 0, "attr": collections.Counter(),
        "flip": 0, "variant_seen": collections.defaultdict(set),
    })
    for f in sorted(Path(p) for p in glob.glob(str(_ROOT / pattern))):
        verds, raw = _load(f)
        rows = score_all(bundles, verds)

        by_judge_bundle = collections.defaultdict(lambda: collections.defaultdict(list))
        for r in rows:
            by_judge_bundle[r.judge][r.bundle_id].append(r.score)
            a = agg[r.judge]
            if r.score == 0.0 and r.attribution:
                a["attr"][r.attribution] += 1
            # "flip" = the same flight answered differently under different wording alone.
            a["variant_seen"][r.bundle_id].add(r.predicted)

        for judge, per_bundle in by_judge_bundle.items():
            means = [sum(v) / len(v) for v in per_bundle.values()]
            agg[judge]["acc_sum"] += sum(means) / len(means)
            agg[judge]["runs"] += 1

        for v in raw:
            a = agg[v["judge"]]
            a["tin"] += v.get("tokens_in") or 0
            a["tout"] += v.get("tokens_out") or 0
            a["calls"] += v.get("calls") or 0
            a["wall"] += v.get("wall_ms") or 0.0
            a["n"] += 1
    return agg


def report_cost(bundles, only: str) -> None:
    print("=" * 100)
    print("1. COST PER CORRECT ANSWER")
    print("   The claim is that an expensive method must beat a free rule AT EQUAL COST.")
    print("   tok/correct = mean tokens per judgement divided by bundle-level accuracy:")
    print("   what you spend to buy one right answer, which is the number that decides it.")
    print("=" * 100)
    for label, pattern in ARMS:
        agg = collect(bundles, pattern)
        if not agg:
            continue
        print(f"\n  {label}")
        print(f"    {'judge':6} {'judg':>5} {'tok/judg':>9} {'calls':>6} {'wall_s':>7} "
              f"{'acc':>6} {'tok/correct':>12}")
        for judge in sorted(agg):
            a = agg[judge]
            if not a["n"]:
                continue
            acc = a["acc_sum"] / a["runs"] if a["runs"] else 0.0
            per = (a["tin"] + a["tout"]) / a["n"]
            eff = f"{per / acc:>12,.0f}" if acc > 0 else f"{'--':>12}"
            print(f"    {judge:6} {a['n']:>5} {per:>9,.0f} {a['calls']/a['n']:>6.2f} "
                  f"{a['wall']/a['n']/1000:>7.2f} {acc:>6.2f} {eff}")
        b1, b3 = agg.get("B1"), agg.get("B3")
        if b1 and b3 and b1["n"] and b3["n"]:
            a1 = b1["acc_sum"] / b1["runs"]
            a3 = b3["acc_sum"] / b3["runs"]
            if a1 > 0 and a3 > 0:
                e1 = (b1["tin"] + b1["tout"]) / b1["n"] / a1
                e3 = (b3["tin"] + b3["tout"]) / b3["n"] / a3
                print(f"    -> B3 costs {e3/e1:.2f}x B1 per correct answer "
                      f"(accuracy {a1:.2f} -> {a3:.2f})")


def report_attribution(bundles, only: str) -> None:
    print()
    print("=" * 100)
    print("2. WHY THE ZEROS HAPPENED")
    print("   A miss attributed to HARNESS is a transport or tool failure, NOT the model")
    print("   reasoning badly. Reporting accuracy without this invites the reader to score")
    print("   the model for the harness's mistakes.")
    print("=" * 100)
    for label, pattern in ARMS:
        agg = collect(bundles, pattern)
        rows = [(j, a) for j, a in sorted(agg.items()) if sum(a["attr"].values())]
        if not rows:
            continue
        print(f"\n  {label}")
        for judge, a in rows:
            total = sum(a["attr"].values())
            parts = "  ".join(f"{k}={v}" for k, v in sorted(a["attr"].items()))
            print(f"    {judge:6} {total:>3} zero-scored judgements:  {parts}")


def report_variants(bundles, only: str) -> None:
    print()
    print("=" * 100)
    print("3. PROMPT-VARIANT SENSITIVITY")
    print("   Same flight, same evidence, three different wordings. A judge that answers")
    print("   differently across them is unstable to phrasing -- which is a robustness")
    print("   property no accuracy column shows.")
    print("=" * 100)
    for label, pattern in ARMS:
        agg = collect(bundles, pattern)
        printed = False
        for judge in sorted(agg):
            seen = agg[judge]["variant_seen"]
            if not seen:
                continue
            unstable = sum(1 for answers in seen.values() if len(answers) > 1)
            if not printed:
                print(f"\n  {label}")
                printed = True
            print(f"    {judge:6} {unstable}/{len(seen)} flights answered inconsistently "
                  f"across wordings")


def detectability(max_n: int = 12) -> None:
    """Best achievable p-value at n per arm, under PERFECT separation.

    Fisher's exact test on a 2x2 where one arm is all-correct and the other all-wrong. If even
    that cannot clear a threshold, then NO result at that n can -- the ceiling is a property of
    the design, not of the effect.

    This is the structural reason the significance claim was retracted. It was not an arithmetic
    slip that better analysis could rescue: with 3 bundles per arm the two-sided p-value cannot
    go below 0.10, so the comparison could never have been significant however the numbers fell.
    More models and more runs do not move this. Only more FLIGHTS do.
    """
    print()
    print("=" * 100)
    print("4. WHAT THIS DESIGN COULD EVER DETECT")
    print("   Best possible two-sided Fisher exact p at n bundles per arm, assuming a PERFECT")
    print("   split (one judge right on every flight, the other wrong on every flight).")
    print("   No real result can beat these -- they are the ceiling.")
    print("=" * 100)
    print(f"\n    {'n per arm':>10} {'best 1-sided p':>15} {'best 2-sided p':>15}   verdict")
    for n in range(2, max_n + 1):
        one = 1.0 / math.comb(2 * n, n)
        two = min(1.0, 2 * one)
        verdict = "cannot reach p<0.05" if two >= 0.05 else "can reach p<0.05"
        mark = "  <-- this project's compass-only comparisons" if n == 3 else ""
        print(f"    {n:>10} {one:>15.5f} {two:>15.5f}   {verdict}{mark}")
    print()
    print("    At n=3 the best two-sided p is 0.100. The design cannot produce a significant")
    print("    two-sided result at any effect size. This is why 'the difference is significant'")
    print("    was retracted, and why a SECOND FAULT -- not more models or more runs -- is the")
    print("    binding constraint on everything this project claims about model behaviour.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundles", default="bundles")
    ap.add_argument("--only", default="compass_offset")
    args = ap.parse_args()

    bundles = load_all(str(_ROOT / args.bundles), only=[args.only])
    print(f"{len(bundles)} bundles ({args.only}); all figures below come from committed "
          f"verdict files -- nothing is spent to run this.\n")
    report_cost(bundles, args.only)
    report_attribution(bundles, args.only)
    report_variants(bundles, args.only)
    detectability()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
