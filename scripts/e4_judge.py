#!/usr/bin/env python3
"""Run every judge over every bundle, under every prompt variant (Phase E4).

    python scripts/e4_judge.py --bundles bundles --dry-run
    python scripts/e4_judge.py --bundles bundles --out verdicts.json

Offline. Reads captured flights from disk, writes verdicts. No simulator involved -- that is the
point of the capture/judge split, and it is what makes ~100 judgments affordable.

**Two passes, and the order is not optional.** B3 runs first so its actual token spend can be
measured; only then is B2's sample count chosen to match it. Running them in the other order
would mean picking B2's budget before knowing what it is supposed to match, which is how a
controlled comparison quietly stops being one. The achieved match is written into the output and
printed -- an intended tolerance with no measurement behind it is a claim, not a control.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_FLIGHTDX_SRC = _ROOT.parent / "ardupilot-log-analyzer" / "src"
if _FLIGHTDX_SRC.exists():
    sys.path.insert(1, str(_FLIGHTDX_SRC))

from sentinel.budget import Budget, k_for_budget, spend_match
from sentinel.bundle import load_all
from sentinel.judges import Verdict
from sentinel.judges.agent import AgentJudge
from sentinel.judges.deterministic import DeterministicJudge
from sentinel.judges.model import DryRunClient
from sentinel.judges.prompts import VARIANTS


def build_client(args):
    """Return a model client, or None if only B0 can run.

    A missing key is not an error: B0 is a complete judge on its own and the deterministic half
    of the table is worth producing by itself. It is reported rather than assumed, so nobody
    reads a B0-only table as though the agent had been measured and lost.
    """
    if args.dry_run:
        return DryRunClient()
    try:
        from sentinel.judges.llm import build_default_client  # step 6, not yet written
    except ImportError:
        return None
    return build_default_client(args.model)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundles", default="bundles")
    ap.add_argument("--out", default="verdicts.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="use the free stub client; exercises the whole sweep at zero cost")
    ap.add_argument("--model", default=None)
    ap.add_argument("--variants", default="v1,v2,v3")
    ap.add_argument("--max-tokens", type=int, default=20_000)
    ap.add_argument("--max-calls", type=int, default=12)
    args = ap.parse_args()

    bundles = load_all(args.bundles)
    if not bundles:
        print(f"no bundles in {args.bundles}/ -- capture some first:\n"
              f"  python scripts/r7_r8_scenarios.py --bundles {args.bundles} --repeat 3",
              file=sys.stderr)
        return 1

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    unknown = [v for v in variants if v not in VARIANTS]
    if unknown:
        print(f"unknown prompt variants: {unknown}; have {sorted(VARIANTS)}", file=sys.stderr)
        return 1

    client = build_client(args)
    verdicts: list[Verdict] = []

    print(f"{len(bundles)} bundles x {len(variants)} variants")

    # ---- B0: one run per bundle. No prompt is involved, so variants do not apply.
    b0 = DeterministicJudge()
    for b in bundles:
        verdicts.append(b0.judge(b))
    print(f"  B0  {len(bundles):>3} verdicts   0 tokens")

    if client is None:
        print("\n  no model client (sentinel/judges/llm.py absent and --dry-run not set)")
        print("  wrote B0 only -- the agent has NOT been measured")
    else:
        # ---- B3 first, to measure what the agent actually costs.
        b3 = AgentJudge(client)
        b3_tokens: list[int] = []
        for b in bundles:
            for v in variants:
                verdict = b3.judge(b, Budget(max_tokens=args.max_tokens,
                                             max_calls=args.max_calls), v)
                verdicts.append(verdict)
                b3_tokens.append(verdict.tokens)
        mean_b3 = sum(b3_tokens) / len(b3_tokens) if b3_tokens else 0
        degraded = sum(1 for v in verdicts if v.judge == "B3" and v.degraded)
        print(f"  B3  {len(b3_tokens):>3} verdicts   {mean_b3:>7.0f} tok/judgement   "
              f"{degraded} degraded")

        # ---- B1 and B2, with B2's k derived from B3's measured spend.
        try:
            from sentinel.judges.llm import NSampleJudge, SingleShotJudge  # type: ignore
        except ImportError:
            # Step 6 not built yet. B0+B3 is a partial table, and saying so is the whole job of
            # this branch -- silently omitting the baselines would leave a reader thinking the
            # agent had been compared against something.
            print("  B1/B2 skipped: sentinel/judges/llm.py not built yet")
            print("      the agent has no LLM baseline in this run; the table is incomplete")
            NSampleJudge = SingleShotJudge = None  # type: ignore

        if SingleShotJudge is None:
            b1 = None
        else:
            b1 = SingleShotJudge(client)
        if b1 is not None:
            b1_tokens: list[int] = []
            for b in bundles:
                for v in variants:
                    verdict = b1.judge(b, Budget(max_tokens=args.max_tokens), v)
                    verdicts.append(verdict)
                    b1_tokens.append(verdict.tokens)
            per_sample = (sum(b1_tokens) / len(b1_tokens)) if b1_tokens else 0
            print(f"  B1  {len(b1_tokens):>3} verdicts   {per_sample:>7.0f} tok/judgement")

            k = k_for_budget(int(mean_b3), int(per_sample))
            b2 = NSampleJudge(client, k=k)
            b2_tokens: list[int] = []
            for b in bundles:
                for v in variants:
                    verdict = b2.judge(b, Budget(max_tokens=args.max_tokens * 4), v)
                    verdicts.append(verdict)
                    b2_tokens.append(verdict.tokens)
            mean_b2 = sum(b2_tokens) / len(b2_tokens) if b2_tokens else 0
            match = spend_match(int(mean_b3), int(mean_b2))
            print(f"  B2  {len(b2_tokens):>3} verdicts   {mean_b2:>7.0f} tok/judgement   "
                  f"k={k}  spend match {match:+.1%} vs B3")
            if abs(match) > 0.10:
                print("      NOTE: outside the +/-10% target. Reported, not hidden -- "
                      "the comparison is weaker than intended.")

    payload = {
        "bundles": [b.bundle_id for b in bundles],
        "variants": variants,
        "client": getattr(client, "name", None),
        "verdicts": [v.model_dump(mode="json") for v in verdicts],
    }
    Path(args.out).write_text(json.dumps(payload, indent=1))
    print(f"\nwrote {len(verdicts)} verdicts to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
