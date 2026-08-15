#!/usr/bin/env python3
"""Prevalence sweep: how many models read advisory ORDER as causation?

    python -u scripts/e4_prevalence.py --list-models
    python -u scripts/e4_prevalence.py --models-from-gateway 20 --dry-run
    python -u scripts/e4_prevalence.py --models-file models.txt --runs 5

WHY THIS EXISTS, and what it is not.

Everything else in this repo is bounded by 3 flights of one fault, and adding models does not
move that -- a third opinion about the same three flights does not narrow a [0.44-1.00] interval.
This asks a DIFFERENT question, whose sample unit is the MODEL rather than the flight:

    given a scenario where the symptom is advised 1.0 s before its own cause,
    how often does a frontier model name the symptom as the root cause?

n = models here. So this is the one substantial result available without capturing new flights,
and it is also the only way to get enough points to say anything about WHICH models fall for it.
Two points cannot support a predictor. Twenty can support a description.

It reproduces the TIMED arm exactly as measured on 2026-08-15 -- `--judges B0,B3
--withhold-tools evidence_untimed --offer-tools detector_evidence`, tool count held at 2, only
the presence of `t` distinguishing it from SPECS. Those flags are not options here. A prevalence
number is meaningless if each model was asked a slightly different question, so the arm is
hard-coded and written into every output file.

THE HEADLINE METRIC IS NOT ACCURACY. It is `sym`, the count of judgements naming a symptom as the
root cause, out of 9. Accuracy conflates "fell for the ordering trap" with "was confused for some
other reason", and the trap is the thing being measured. Both are reported.

Design notes that are not preferences:

* Each (model, run) is a subprocess writing its own file. A model that hangs, rate-limits or
  returns malformed tool calls loses its own cell and nothing else -- with 20 models the odds
  that all 20 behave are low, and a sweep that dies on model 14 has cost real money for nothing.
* Resumable by default: an existing output file is skipped. Rule 3 in this project is "never edit
  code while an experiment is running", which is only livable if a crash does not mean starting
  over.
* B0 is run for every model and MUST be 0.00. It is deterministic code with no model in it, so a
  non-zero B0 means the scenario is not ambiguous for that run and nothing else in the cell is
  readable. It is checked, not assumed.
* Console output is ASCII (Windows cp1252 renders anything else as `?`). Use `python -u`.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import statistics
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
_FLIGHTDX_SRC = _ROOT.parent / "ardupilot-log-analyzer" / "src"
if _FLIGHTDX_SRC.exists():
    sys.path.insert(1, str(_FLIGHTDX_SRC))

from sentinel.bundle import load_all           # noqa: E402
from sentinel.judges import Verdict            # noqa: E402
from sentinel.score import score_all           # noqa: E402

# The timed arm, frozen. See module docstring.
ARM = dict(
    only="compass_offset",
    variants="v1,v2,v3",
    judges="B0,B3",
    withhold_tools="evidence_untimed",
    offer_tools="detector_evidence",
)
DEFAULT_BASE_URL = "https://api.llmapi.ai/v1"


def slug(model: str) -> str:
    """Filesystem-safe cell name. Model ids carry slashes, colons and dots."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_")


# ---------------------------------------------------------------- model discovery

def fetch_models(base_url: str, api_key: str) -> list[str]:
    """GET {base_url}/models. Ids are never invented -- a guessed id is a 404 and a wasted slot."""
    req = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        payload = json.loads(r.read().decode())
    items = payload.get("data", payload if isinstance(payload, list) else [])
    return sorted({it["id"] for it in items if isinstance(it, dict) and "id" in it})


# Families worth one representative each. A prevalence number over 20 near-identical checkpoints
# of one vendor measures that vendor, not the population -- so selection spreads across families
# and takes the largest/most capable member of each rather than whatever sorts first.
_FAMILY_HINTS = (
    "gpt", "claude", "gemini", "qwen", "kimi", "llama", "mistral", "deepseek", "grok",
    "command", "yi", "glm", "minimax", "nova", "phi", "gemma", "jamba", "reka", "sonar",
)
_SKIP = ("embed", "whisper", "tts", "audio", "image", "vision-only", "rerank", "moderation",
         "guard", "-vl-", "diffusion")


def pick_spread(available: list[str], n: int) -> list[str]:
    """One representative per family first, then fill. Reported, never silent."""
    usable = [m for m in available if not any(s in m.lower() for s in _SKIP)]
    by_family: dict[str, list[str]] = collections.defaultdict(list)
    for m in usable:
        low = m.lower()
        fam = next((h for h in _FAMILY_HINTS if h in low), "other")
        by_family[fam].append(m)
    picked: list[str] = []
    # round-robin across families so a 200-model vendor cannot dominate the sample
    while len(picked) < n:
        progressed = False
        for fam in sorted(by_family):
            if len(picked) >= n:
                break
            if by_family[fam]:
                picked.append(by_family[fam].pop(0))
                progressed = True
        if not progressed:
            break
    return picked


def pick_families(available: list[str], families: list[str], per_family: int) -> list[str]:
    """Resolve named families to real gateway ids.

    Ids are matched against what the gateway actually offers rather than written down here:
    a hardcoded id that has been renamed or retired is a 404, which costs a slot and looks in
    the results table exactly like a model that failed the task.

    A family that matches nothing is returned as an explicit miss by the caller, not dropped --
    silently sweeping 5 families when 6 were asked for is how a coverage claim stops being true.
    """
    usable = [m for m in available if not any(s in m.lower() for s in _SKIP)]
    picked: list[str] = []
    for fam in families:
        hits = [m for m in usable if fam.lower() in m.lower()]
        # Prefer the plainest id in the family: fewest separators, then shortest, then
        # alphabetical. Dated or size-suffixed variants ("-2026-01-14", "-32b-instruct") are
        # usually checkpoints of the same model, and one representative per family is the point.
        hits.sort(key=lambda m: (m.count("-") + m.count("_") + m.count(":"), len(m), m))
        picked.extend(hits[:per_family])
    return picked


# ---------------------------------------------------------------- running

def cell_path(outdir: Path, model: str, run: int) -> Path:
    return outdir / f"{slug(model)}__run{run}.json"


def run_cell(model: str, run: int, outdir: Path, base_url: str, bundles_dir: str,
             dry_run: bool, min_interval: float, timeout_s: int) -> tuple[str, str]:
    """Run one (model, run). Returns (status, detail). Never raises."""
    out = cell_path(outdir, model, run)
    if out.exists() and out.stat().st_size > 0:
        return "skip", "already present"

    cmd = [sys.executable, "-u", str(_ROOT / "scripts" / "e4_judge.py"),
           "--bundles", bundles_dir,
           "--only", ARM["only"],
           "--variants", ARM["variants"],
           "--judges", ARM["judges"],
           "--withhold-tools", ARM["withhold_tools"],
           "--offer-tools", ARM["offer_tools"],
           "--min-interval", str(min_interval),
           "--out", str(out)]
    if dry_run:
        cmd.append("--dry-run")
    else:
        cmd += ["--provider", "openai", "--model", model, "--base-url", base_url]

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=str(_ROOT))
    except subprocess.TimeoutExpired:
        out.unlink(missing_ok=True)
        return "timeout", f"exceeded {timeout_s}s"
    if p.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        tail = (p.stderr or p.stdout or "").strip().splitlines()
        return "error", (tail[-1][:160] if tail else f"exit {p.returncode}")
    return "ok", ""


# ---------------------------------------------------------------- scoring

def load_verdicts(path: Path) -> list[Verdict]:
    raw = json.loads(path.read_text())
    vs = raw if isinstance(raw, list) else raw.get("verdicts", [])
    return [Verdict.model_validate(v) for v in vs]


def score_cell(bundles, path: Path) -> dict | None:
    """Per-judge bundle-level accuracy and symptom-as-root count for one run."""
    try:
        rows = score_all(bundles, load_verdicts(path))
    except Exception:
        return None
    per = collections.defaultdict(lambda: {"scores": collections.defaultdict(list), "sym": 0,
                                           "n": 0, "deg": 0})
    for r in rows:
        d = per[r.judge]
        d["scores"][r.bundle_id].append(r.score)
        d["sym"] += int(r.named_symptom_as_root)
        d["n"] += 1
        d["deg"] += int(r.degraded)
    out = {}
    for judge, d in per.items():
        bundle_means = [sum(v) / len(v) for v in d["scores"].values()]
        out[judge] = {"acc": sum(bundle_means) / len(bundle_means), "sym": d["sym"],
                      "n": d["n"], "deg": d["deg"]}
    return out


def summarise(bundles, models: list[str], runs: int, outdir: Path) -> list[dict]:
    rows = []
    for model in models:
        cells = [score_cell(bundles, cell_path(outdir, model, r)) for r in range(1, runs + 1)]
        cells = [c for c in cells if c]
        if not cells:
            rows.append({"model": model, "runs": 0, "status": "no usable runs"})
            continue
        b3 = [c["B3"] for c in cells if "B3" in c]
        b0 = [c["B0"] for c in cells if "B0" in c]
        if not b3:
            rows.append({"model": model, "runs": len(cells), "status": "no B3"})
            continue
        accs = [x["acc"] for x in b3]
        syms = [x["sym"] for x in b3]
        b0_bad = [x["acc"] for x in b0 if abs(x["acc"]) > 1e-9]
        rows.append({
            "model": model, "runs": len(b3), "status": "ok",
            "acc_mean": sum(accs) / len(accs),
            "acc_spread": max(accs) - min(accs),
            "sym_mean": sum(syms) / len(syms),
            "sym_max_of": b3[0]["n"],
            "degraded": sum(x["deg"] for x in b3),
            # Rule: B0 is deterministic and must be 0.00. If it is not, the cell is unreadable.
            "b0_violation": round(max(b0_bad), 3) if b0_bad else None,
        })
    return rows


def print_table(rows: list[dict], runs: int) -> None:
    print()
    print("=" * 92)
    print("PREVALENCE OF THE ORDERING TRAP -- timed arm, B3, compass_offset")
    print("  sym = judgements naming a SYMPTOM as the root cause (the trap), mean over runs")
    print("  acc = bundle-level root-cause accuracy, mean over runs")
    print("=" * 92)
    print(f"{'model':<38} {'runs':>4} {'sym/9':>7} {'acc':>6} {'spread':>7} {'deg':>4}  note")
    print("-" * 92)
    ok = [r for r in rows if r.get("status") == "ok"]
    for r in sorted(ok, key=lambda x: (-x["sym_mean"], x["acc_mean"])):
        note = ""
        if r["b0_violation"] is not None:
            note = f"B0={r['b0_violation']} NOT 0.00 -- cell unreadable"
        elif r["runs"] < runs:
            note = f"only {r['runs']} of {runs} runs"
        print(f"{r['model'][:38]:<38} {r['runs']:>4} {r['sym_mean']:>7.2f} "
              f"{r['acc_mean']:>6.2f} {r['acc_spread']:>7.2f} {r['degraded']:>4}  {note}")
    bad = [r for r in rows if r.get("status") != "ok"]
    if bad:
        print("-" * 92)
        for r in bad:
            print(f"{r['model'][:38]:<38} {'-':>4} {'-':>7} {'-':>6} {'-':>7} {'-':>4}  "
                  f"{r['status']}")
    if ok:
        caught = [r for r in ok if r["sym_mean"] >= 1.0]
        clean = [r for r in ok if r["sym_mean"] == 0.0]
        print("-" * 92)
        print(f"  {len(caught)} of {len(ok)} models named a symptom as root at least once per run")
        print(f"  {len(clean)} of {len(ok)} never did, in any run")
        print(f"  median sym/9 = {statistics.median([r['sym_mean'] for r in ok]):.2f}")
    print()
    print("  n here is MODELS, not flights. This says nothing about the 3-flight limit")
    print("  that bounds every other number in this project.")
    print()


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bundles", default="bundles")
    ap.add_argument("--outdir", default="results/prevalence")
    ap.add_argument("--runs", type=int, default=5,
                    help="repeats per model. Five, because the measured spread is 0.11 and "
                         "single runs produced three of this project's six retractions")
    ap.add_argument("--models", default=None, help="comma-separated model ids")
    ap.add_argument("--models-file", default=None, help="one model id per line; # comments ok")
    ap.add_argument("--models-from-gateway", type=int, default=None,
                    help="discover ids from {base-url}/models and pick N spread across families")
    ap.add_argument("--families", default=None,
                    help="comma-separated family substrings (e.g. "
                         "deepseek,qwen,grok,kimi,sonnet,devstral). Ids are resolved against "
                         "what the gateway actually offers; a family matching nothing is "
                         "reported, not silently skipped")
    ap.add_argument("--per-family", type=int, default=1,
                    help="how many ids to take per family (default 1 representative)")
    ap.add_argument("--list-models", action="store_true",
                    help="print every id the gateway offers, then exit")
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL))
    ap.add_argument("--dry-run", action="store_true",
                    help="stub client, zero cost. Exercises the whole sweep including scoring")
    ap.add_argument("--min-interval", type=float, default=0.5)
    ap.add_argument("--timeout", type=int, default=900, help="seconds per (model, run) cell")
    ap.add_argument("--summarise-only", action="store_true",
                    help="re-score what is already on disk; runs nothing")
    args = ap.parse_args()

    key = os.environ.get("OPENAI_API_KEY", "")
    if args.list_models:
        if not key:
            print("OPENAI_API_KEY is not set", file=sys.stderr)
            return 2
        for m in fetch_models(args.base_url, key):
            print(m)
        return 0

    # -- resolve the model list
    models: list[str] = []
    if args.models:
        models = [m.strip() for m in args.models.split(",") if m.strip()]
    elif args.models_file:
        text = Path(args.models_file).read_text(encoding="utf-8")
        models = [ln.strip() for ln in text.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    elif args.families:
        wanted_fams = [f.strip() for f in args.families.split(",") if f.strip()]
        if args.dry_run:
            models = [f"dry-{f}" for f in wanted_fams for _ in range(args.per_family)]
        else:
            if not key:
                print("OPENAI_API_KEY is not set; cannot discover models", file=sys.stderr)
                return 2
            available = fetch_models(args.base_url, key)
            models = pick_families(available, wanted_fams, args.per_family)
            found = {f for f in wanted_fams
                     if any(f.lower() in m.lower() for m in models)}
            missing = [f for f in wanted_fams if f not in found]
            print(f"gateway offers {len(available)} ids; resolved {len(models)} "
                  f"across {len(found)} of {len(wanted_fams)} families")
            for m in models:
                print(f"    {m}")
            if missing:
                # Loud, because a family that silently vanished turns "6 families tested" into
                # a false statement in whatever gets published from this.
                print(f"  NO MATCH for {missing} -- these families were NOT swept. "
                      f"Run --list-models to see what the gateway calls them.")
    elif args.models_from_gateway:
        if args.dry_run:
            models = [f"dry-model-{i:02d}" for i in range(1, args.models_from_gateway + 1)]
        else:
            if not key:
                print("OPENAI_API_KEY is not set; cannot discover models", file=sys.stderr)
                return 2
            available = fetch_models(args.base_url, key)
            models = pick_spread(available, args.models_from_gateway)
            print(f"gateway offers {len(available)} ids; selected {len(models)} "
                  f"spread across families")
    if not models:
        print("no models given: use --models, --models-file or --models-from-gateway",
              file=sys.stderr)
        return 2

    if not args.dry_run and not args.summarise_only and not key:
        print("OPENAI_API_KEY is not set. Set it and re-run; nothing was spent.", file=sys.stderr)
        return 2

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    bundles = load_all(args.bundles, only=[ARM["only"]])
    print(f"arm: judges={ARM['judges']} withhold={ARM['withhold_tools']} "
          f"offer={ARM['offer_tools']} variants={ARM['variants']}")
    print(f"{len(bundles)} bundles x 3 variants x {args.runs} runs x {len(models)} models "
          f"= {len(bundles) * 3 * args.runs * len(models)} judgements")

    if not args.summarise_only:
        total = len(models) * args.runs
        done = 0
        for model in models:
            for run in range(1, args.runs + 1):
                done += 1
                status, detail = run_cell(model, run, outdir, args.base_url, args.bundles,
                                          args.dry_run, args.min_interval, args.timeout)
                mark = {"ok": "ok  ", "skip": "skip", "error": "ERR ", "timeout": "TIME"}[status]
                print(f"[{done:>3}/{total}] {mark} {model[:40]:<40} run{run}"
                      + (f"  {detail}" if detail else ""))

    rows = summarise(bundles, models, args.runs, outdir)
    print_table(rows, args.runs)
    summary = outdir / "SUMMARY.json"
    summary.write_text(json.dumps({"arm": ARM, "runs": args.runs, "base_url": args.base_url,
                                   "rows": rows}, indent=1), encoding="utf-8")
    print(f"wrote {summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
