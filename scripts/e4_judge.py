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
from sentinel.judges.tools import BundleTools


def build_client(args):
    """Return a model client, or None if only B0 can run.

    A missing key is not an error: B0 is a complete judge on its own and the deterministic half
    of the table is worth producing by itself. It is reported rather than assumed, so nobody
    reads a B0-only table as though the agent had been measured and lost.

    Provider is explicit rather than inferred. This function previously reached only for the
    Anthropic client, so on a machine with no ANTHROPIC_API_KEY a live sweep died in
    `build_default_client` -- which raises RuntimeError, not the ImportError caught below. That
    is why every live number so far came from a hand-written loop instead of this script, and
    why gemini.py existed with nothing wired to it.

    A credentials failure still degrades to B0 rather than aborting: the same reasoning as the
    ImportError path. A partial table that says it is partial beats no table.
    """
    if args.dry_run:
        return DryRunClient()
    try:
        if args.provider == "gemini":
            from sentinel.judges.gemini import build_gemini_client
            return build_gemini_client(args.model, min_interval_s=args.min_interval)
        if args.provider == "openai":
            # Any OpenAI-compatible endpoint: GPT, Grok, OpenRouter, a local server. This is the
            # path to a SECOND model, which is what decides whether the ordering finding is a
            # property of agents or of one model.
            from sentinel.judges.openai_compat import build_openai_client
            return build_openai_client(args.model, base_url=args.base_url,
                                       min_interval_s=args.min_interval)
        from sentinel.judges.llm import build_default_client
        return build_default_client(args.model)
    except (ImportError, RuntimeError) as exc:
        print(f"  no model client ({args.provider}): {exc}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundles", default="bundles")
    ap.add_argument("--out", default="verdicts.json")
    ap.add_argument("--dry-run", action="store_true",
                    help="use the free stub client; exercises the whole sweep at zero cost")
    ap.add_argument("--provider", choices=("anthropic", "gemini", "openai"), default="anthropic",
                    help="which model backend B1/B2/B3 speak to. The provider is written into "
                         "the output as `client` -- a published table must name the model it "
                         "measured, and gemini is not claude")
    ap.add_argument("--model", default=None)
    ap.add_argument("--base-url", default=None,
                    help="OpenAI-compatible endpoint (--provider openai). Omit for OpenAI; "
                         "https://api.x.ai/v1 for Grok, https://openrouter.ai/api/v1 for "
                         "OpenRouter, http://localhost:11434/v1 for Ollama. Also read from "
                         "OPENAI_BASE_URL")
    ap.add_argument("--min-interval", type=float, default=1.0,
                    help="seconds between API requests, enforced across all judge tiers "
                         "(gemini only). Raise it if degradations reappear -- a degraded "
                         "judgement is a lost measurement, so waiting is always the cheaper "
                         "failure. 0 disables pacing")
    ap.add_argument("--only", default=None,
                    help="comma-separated substrings matched against the bundle's SCENARIO "
                         "name (not its filename -- a RunBundle does not carry its path). "
                         "Scoping the sweep to the scenarios under test is the difference "
                         "between a cheap run and judging the whole archive")
    ap.add_argument("--variants", default="v1,v2,v3")
    ap.add_argument("--judges", default="B0,B1,B2,B3",
                    help="which tiers to run. B2 needs B1 in the same run -- its k is derived "
                         "from B1's measured per-sample cost, and reusing a number from a "
                         "different run would silently break the matched-spend control")
    ap.add_argument("--offer-tools", default=None,
                    help="comma-separated OPTIONAL tools added to B3's offered set (e.g. "
                         "evidence_untimed). Kept separate from the default five so the "
                         "published table's tool set never changes underneath it")
    ap.add_argument("--withhold-tools", default=None,
                    help="comma-separated tool names removed from B3's offered set (ablation). "
                         "`--withhold-tools list_advisories,ordering` is the experiment that "
                         "tests whether detection-order metadata is what makes the agent name a "
                         "symptom as the root cause")
    ap.add_argument("--max-tokens", type=int, default=20_000)
    ap.add_argument("--max-calls", type=int, default=12)
    args = ap.parse_args()

    wanted = [s.strip() for s in (args.only or "").split(",") if s.strip()]
    bundles = load_all(args.bundles, only=wanted or None)
    if wanted:
        # Say what was scoped out. A sweep that judged 9 of 25 bundles and printed no sign of it
        # is how a scoped run gets read as a full one.
        total = len(list(Path(args.bundles).glob("*.json")))
        print(f"--only {args.only}: judging {len(bundles)} of {total} files in {args.bundles}/")
        if not bundles:
            print(f"no bundle filename matched {wanted}", file=sys.stderr)
            return 1
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

    judges = {j.strip().upper() for j in args.judges.split(",") if j.strip()}
    unknown_j = judges - {"B0", "B1", "B2", "B3"}
    if unknown_j:
        print(f"unknown judges: {sorted(unknown_j)}", file=sys.stderr)
        return 1
    withheld = tuple(t.strip() for t in (args.withhold_tools or "").split(",") if t.strip())
    if withheld:
        known = {s["name"] for s in BundleTools.SPECS}
        bad = [t for t in withheld if t not in known]
        if bad:
            print(f"--withhold-tools names {bad}, not in {sorted(known)}", file=sys.stderr)
            return 1
        print(f"ABLATION: B3 runs without {list(withheld)} "
              f"({len(known) - len(withheld)} of {len(known)} default tools offered)")
    offered = tuple(t.strip() for t in (args.offer_tools or "").split(",") if t.strip())
    if offered:
        optional = {s["name"] for s in BundleTools.OPTIONAL_SPECS}
        bad = [t for t in offered if t not in optional]
        if bad:
            print(f"--offer-tools names {bad}, not in {sorted(optional)}", file=sys.stderr)
            return 1
        print(f"ADDED: B3 is also offered {list(offered)}")

    client = build_client(args)
    verdicts: list[Verdict] = []

    print(f"{len(bundles)} bundles x {len(variants)} variants   judges={sorted(judges)}")

    # ---- B0: one run per bundle. No prompt is involved, so variants do not apply.
    if "B0" in judges:
        b0 = DeterministicJudge()
        for b in bundles:
            verdicts.append(b0.judge(b))
        print(f"  B0  {len(bundles):>3} verdicts   0 tokens")

    if client is None:
        print("\n  no model client (sentinel/judges/llm.py absent and --dry-run not set)")
        print("  wrote B0 only -- the agent has NOT been measured")
    else:
        # ---- B3 first, to measure what the agent actually costs.
        b3_tokens: list[int] = []
        if "B3" in judges:
            b3 = AgentJudge(client, withhold=withheld, offer=offered)
            for b in bundles:
                for v in variants:
                    verdict = b3.judge(b, Budget(max_tokens=args.max_tokens,
                                                 max_calls=args.max_calls), v)
                    verdicts.append(verdict)
                    b3_tokens.append(verdict.tokens)
            degraded = sum(1 for v in verdicts if v.judge == "B3" and v.degraded)
            print(f"  B3  {len(b3_tokens):>3} verdicts   "
                  f"{sum(b3_tokens) / len(b3_tokens) if b3_tokens else 0:>7.0f} tok/judgement   "
                  f"{degraded} degraded")
        mean_b3 = sum(b3_tokens) / len(b3_tokens) if b3_tokens else 0

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

        if SingleShotJudge is None or "B1" not in judges:
            b1 = None
            if "B2" in judges and "B1" not in judges:
                # Said out loud rather than skipped quietly: B2's whole claim is "same spend as
                # B3", and k comes from B1's measured per-sample cost in THIS run.
                print("  B2 skipped: it needs B1 in the same run to derive k")
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

            if "B2" not in judges:
                k = 0
            else:
                k = k_for_budget(int(mean_b3), int(per_sample))
            b2 = NSampleJudge(client, k=k) if k else None
            b2_tokens: list[int] = []
        if b1 is not None and b2 is not None:
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
        # Provenance for the ablation. A verdict file that does not record which tools B3 was
        # offered is not comparable to any other verdict file.
        "judges": sorted(judges),
        "withheld_tools": list(withheld),
        "offered_optional_tools": list(offered),
        "verdicts": [v.model_dump(mode="json") for v in verdicts],
    }
    Path(args.out).write_text(json.dumps(payload, indent=1))
    print(f"\nwrote {len(verdicts)} verdicts to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
