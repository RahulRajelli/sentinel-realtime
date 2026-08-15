#!/usr/bin/env python3
"""Regenerate the measured table from verdicts.json (Phase E4).

    python scripts/e4_report.py --bundles bundles --verdicts verdicts.json

**No number in the README is ever typed by hand.** Everything printed here is derived from the
scored verdicts, so the published table cannot drift away from the data that produced it. That
is the whole reason `--markdown` exists: paste, do not transcribe.

The report is allowed to conclude that the experiment was uninformative, and says so loudly when
it was. Two ways that happens:

  * `ambiguity_worked()` -- if the deterministic baseline scores 100%, no agent can beat it and
    every other row is a tie at higher cost, regardless of how good the agent looks;
  * a missing agent -- a B0-only run is a table with one row, not evidence that the agent lost.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_FLIGHTDX_SRC = _ROOT.parent / "ardupilot-log-analyzer" / "src"
if _FLIGHTDX_SRC.exists():
    sys.path.insert(1, str(_FLIGHTDX_SRC))

from sentinel.bundle import load_all
from sentinel.judges import Verdict
from sentinel.score import score_all
from sentinel.stats import ambiguity_worked, summarize_all


def fmt_ci(lo: float, hi: float) -> str:
    return f"[{lo:.2f}-{hi:.2f}]"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundles", default="bundles")
    ap.add_argument("--verdicts", default="verdicts.json")
    ap.add_argument("--markdown", action="store_true", help="emit a README-pasteable table")
    ap.add_argument("--only", default=None,
                    help="comma-separated filename substrings, same meaning as in e4_judge.py. "
                         "Pass the SAME value the sweep used: the report scores verdicts against "
                         "bundles, so a wider scope here loads flights no verdict covers")
    args = ap.parse_args()

    wanted = [s.strip() for s in (args.only or "").split(",") if s.strip()]
    bundles = load_all(args.bundles, only=wanted or None)
    payload = json.loads(Path(args.verdicts).read_text())
    verdicts = [Verdict.model_validate(v) for v in payload["verdicts"]]
    rows = score_all(bundles, verdicts)
    stats = summarize_all(rows)

    print("=" * 100)
    print("E4 — MEASURED COMPARISON")
    print("=" * 100)
    print(f"  bundles      : {len(bundles)}  ({len({b.scenario for b in bundles})} scenarios)")
    print(f"  variants     : {', '.join(payload.get('variants', []))}")
    print(f"  model client : {payload.get('client') or 'none — B0 only'}")
    print()

    hdr = (f"{'judge':<6}{'acc':>6} {'95% CI (n=bundles)':<20}{'bundles':>9}{'judg':>6}  "
           f"{'sym':>4}{'hal':>5}{'miss':>6}{'cite':>6}{'deg':>5}  "
           f"{'tok/judge':>10}  {'flip':>6}{'range':>7}")
    print(hdr)
    print("-" * len(hdr))
    for s in stats.values():
        # Bundle-level accuracy and interval are quoted, NOT the judgement-level ones. Three
        # prompt variants of one flight are repeated measures, not three independent trials;
        # quoting the n=27 interval understates uncertainty by about sqrt(3) and previously
        # turned an overlapping comparison into an apparently significant one.
        print(f"{s.judge:<6}{s.accuracy_bundle:>6.2f} {fmt_ci(*s.ci_bundle):<20}"
              f"{s.correct_bundles:>4}/{s.n_bundles:<4}{s.n:>6}  "
              f"{s.named_symptom_as_root:>4}{s.hallucinated:>5}{s.missed:>6}"
              f"{s.citation_failures:>6}{s.degraded:>5}  "
              f"{s.tokens_per_bundle:>10.0f}  "
              f"{s.flip_rate:>6.2f}{s.accuracy_range:>7.2f}")
    print("\n  acc/CI are per BUNDLE (the independent unit). 'judg' is the judgement count;")
    print("  its interval would be ~sqrt(3) too narrow because the 3 prompt variants of a")
    print("  flight are repeated measures, not independent trials.")

    print("\n  sym = named a symptom as the root cause   hal = fault claimed on a clean flight")
    print("  cite = citation did not resolve          deg = spend ceiling tripped")
    print("  flip = answer changed with wording only  range = accuracy spread across variants")

    # ---- per-scenario detail: where the agent actually earns its cost -------------------
    print("\n--- per scenario ---")
    scenarios = sorted({r.scenario for r in rows})
    judges = sorted(stats)
    print(f"  {'scenario':<18}" + "".join(f"{j:>7}" for j in judges))
    for sc in scenarios:
        cells = []
        for j in judges:
            sub = [r for r in rows if r.scenario == sc and r.judge == j]
            cells.append(f"{sum(r.score for r in sub) / len(sub):>7.2f}" if sub else f"{'-':>7}")
        print(f"  {sc:<18}" + "".join(cells))

    # ---- attribution -------------------------------------------------------------------
    print("\n--- why the misses happened (model / harness / environment) ---")
    for s in stats.values():
        if s.attribution:
            total = sum(s.attribution.values())
            parts = "  ".join(f"{k} {v}" for k, v in sorted(s.attribution.items()))
            print(f"  {s.judge:<4} {total:>3} misses:  {parts}")

    # ---- the honesty gates --------------------------------------------------------------
    check = ambiguity_worked(rows)
    print("\n--- is this comparison informative? ---")
    if check.get("warning"):
        print(f"  NO. {check['warning']}")
        print("      Add faults where the first advisory is a symptom, not the root cause.")
    else:
        print(f"  yes — baseline accuracy {check['baseline_accuracy']:.2f}, "
              f"headroom on: {', '.join(check['scenarios_with_headroom'])}")

    if not any(s.tokens_total for s in stats.values()):
        print("\n  NOTE: every judge spent zero tokens. No LLM was measured in this run.")

    n_per_judge = max((s.n for s in stats.values()), default=0)
    if n_per_judge < 12:
        print(f"\n  NOTE: n={n_per_judge} per judge. Intervals are wide by construction; "
              f"capture more flights with --repeat 3 before reading the point estimates.")

    if args.markdown:
        print("\n\n```markdown")
        print("| judge | accuracy | 95% CI | symptom-as-root | hallucinated | degraded "
              "| tok/judgement | flip rate |")
        print("|---|---|---|---|---|---|---|---|")
        for s in stats.values():
            print(f"| {s.judge} | {s.accuracy:.2f} | {fmt_ci(*s.ci)} "
                  f"| {s.named_symptom_as_root}/{s.n} | {s.hallucinated}/{s.n} "
                  f"| {s.degraded}/{s.n} | {s.tokens_per_bundle:.0f} | {s.flip_rate:.2f} |")
        print("```")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
