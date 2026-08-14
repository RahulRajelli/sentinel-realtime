"""Statistics for the published table (Phase E4).

Every number this project publishes carries an interval, because n is small and a bare point
estimate at n=12 is a claim the data cannot support. The job of this file is to make the
uncertainty visible rather than to make the result look good.

Three choices worth defending:

**Wilson, not the normal approximation.** At n=12 with p near 1.0 the normal interval runs past
1.0, which is not a probability. Wilson stays inside [0, 1] and is the standard fix.

**Cohen's kappa, not raw agreement.** *Reliability without Validity* measured a 33-41 point
judge validation gap that raw agreement hides: two raters who both say "correct" 95% of the time
agree 90% of the time by chance alone. Kappa subtracts that chance floor.

**Kappa is allowed to come back undefined, and that is reported.** When both raters assign the
same label to everything -- entirely plausible here, since B0 already scores 4/4 on the easy
scenarios -- expected agreement is 1.0, the denominator vanishes, and kappa is genuinely
undefined rather than perfect. This is the well-known kappa paradox. Returning NaN and printing
"undefined (no label variation)" is the honest outcome; returning 1.0 would be a fabricated
result, and returning 0.0 would be a fabricated failure.

No LLM is involved anywhere in this file. `judges/grader.py` supplies a second rater so kappa has
two opinions to compare; `score.py` remains the authority when they disagree.
"""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Sequence

from sentinel.score import ScoreRow

Z95 = 1.959963984540054


# --- intervals ---------------------------------------------------------------------------

def wilson_interval(successes: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion. Returns (lo, hi), clamped to [0, 1].

    n == 0 returns the whole interval rather than raising: "no data" is a legitimate state for a
    judge that crashed on every bundle, and a report that cannot render it is a worse outcome
    than one that shows [0, 1] and a count of zero.
    """
    if n <= 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, center - half), min(1.0, center + half))


def bootstrap_ci(items: Sequence[Any], statistic, n_boot: int = 2000,
                 seed: int = 0, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap CI for any statistic over a list of items.

    Seeded on purpose: an interval that moved every time the report regenerated would make the
    published table irreproducible, which is the one property the whole capture/judge split
    exists to protect. NaN replicates are dropped -- a resample can legitimately contain no
    label variation, leaving kappa undefined for that draw -- and if too few survive, the
    interval is reported as undefined rather than computed from a handful.
    """
    if not items:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(items)
    draws: list[float] = []
    for _ in range(n_boot):
        sample = [items[rng.randrange(n)] for _ in range(n)]
        try:
            val = statistic(sample)
        except (ZeroDivisionError, ValueError):
            continue
        if val is not None and not math.isnan(val):
            draws.append(val)
    if len(draws) < max(20, n_boot // 20):
        return (float("nan"), float("nan"))
    draws.sort()
    lo = draws[int(alpha / 2 * len(draws))]
    hi = draws[min(len(draws) - 1, int((1 - alpha / 2) * len(draws)))]
    return (lo, hi)


# --- agreement ---------------------------------------------------------------------------

def cohen_kappa(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Cohen's kappa between two raters over the same items. NaN when undefined.

    Undefined means expected agreement is 1.0 -- both raters used a single label throughout, so
    chance agreement is total and there is no room above it to measure. See the module docstring:
    this is reported, not papered over.
    """
    if len(a) != len(b):
        raise ValueError(f"raters disagree on item count: {len(a)} vs {len(b)}")
    n = len(a)
    if n == 0:
        return float("nan")

    po = sum(1 for x, y in zip(a, b) if x == y) / n
    ca, cb = Counter(a), Counter(b)
    pe = sum((ca[k] / n) * (cb[k] / n) for k in set(ca) | set(cb))

    if math.isclose(pe, 1.0):
        return float("nan")
    return (po - pe) / (1.0 - pe)


def kappa_with_ci(pairs: Sequence[tuple[Any, Any]], n_boot: int = 2000,
                  seed: int = 0) -> tuple[float, float, float]:
    """(kappa, lo, hi) over paired rater labels."""
    k = cohen_kappa([x for x, _ in pairs], [y for _, y in pairs])
    lo, hi = bootstrap_ci(pairs, lambda s: cohen_kappa([x for x, _ in s], [y for _, y in s]),
                          n_boot=n_boot, seed=seed)
    return k, lo, hi


# --- per-judge aggregation ----------------------------------------------------------------

@dataclass
class JudgeStats:
    judge: str
    n: int = 0
    correct: int = 0
    accuracy: float = 0.0
    ci: tuple[float, float] = (0.0, 1.0)

    named_symptom_as_root: int = 0
    hallucinated: int = 0
    missed: int = 0
    citation_failures: int = 0
    degraded: int = 0

    tokens_total: int = 0
    tokens_per_bundle: float = 0.0
    wall_ms_mean: float = 0.0

    # Prompt sensitivity. `accuracy_by_variant` is the spread the paper warns about; `flip_rate`
    # is the fraction of flights whose ANSWER changed when only the wording changed -- a judge
    # can hold accuracy steady while flipping half its verdicts, and that is worth knowing.
    accuracy_by_variant: dict[str, float] = field(default_factory=dict)
    accuracy_range: float = 0.0
    flip_rate: float = 0.0

    attribution: dict[str, int] = field(default_factory=dict)

    def line(self) -> str:
        lo, hi = self.ci
        return (f"{self.judge:<4} {self.accuracy:>5.2f} [{lo:.2f}-{hi:.2f}]  "
                f"{self.correct:>3}/{self.n:<3} "
                f"sym {self.named_symptom_as_root:>2}  hal {self.hallucinated:>2}  "
                f"deg {self.degraded:>2}  "
                f"tok/bundle {self.tokens_per_bundle:>7.0f}  "
                f"flip {self.flip_rate:>4.2f}  range {self.accuracy_range:>4.2f}")


def summarize_judge(rows: Sequence[ScoreRow]) -> JudgeStats:
    """Aggregate one judge's rows. Rows must all share a judge id."""
    if not rows:
        return JudgeStats(judge="?")
    judges = {r.judge for r in rows}
    if len(judges) != 1:
        raise ValueError(f"summarize_judge got mixed judges: {sorted(judges)}")

    n = len(rows)
    correct = sum(1 for r in rows if r.score == 1.0)

    by_variant: dict[str, list[ScoreRow]] = defaultdict(list)
    for r in rows:
        by_variant[r.variant].append(r)
    acc_by_variant = {v: sum(1 for r in rs if r.score == 1.0) / len(rs)
                      for v, rs in sorted(by_variant.items())}

    # A flight counts as flipped when the same judge gave different ANSWERS to the same bundle
    # under different wordings. Only bundles seen under 2+ variants are eligible -- B0 has one
    # variant by construction and must not be scored as perfectly stable for that reason.
    answers: dict[str, set[Any]] = defaultdict(set)
    for r in rows:
        answers[r.bundle_id].add(r.predicted)
    eligible = [bid for bid in answers if len({r.variant for r in rows if r.bundle_id == bid}) > 1]
    flips = sum(1 for bid in eligible if len(answers[bid]) > 1)

    attribution = Counter(r.attribution for r in rows if r.attribution)

    return JudgeStats(
        judge=rows[0].judge,
        n=n,
        correct=correct,
        accuracy=correct / n,
        ci=wilson_interval(correct, n),
        named_symptom_as_root=sum(1 for r in rows if r.named_symptom_as_root),
        hallucinated=sum(1 for r in rows if r.hallucinated),
        missed=sum(1 for r in rows if r.missed),
        citation_failures=sum(1 for r in rows if not r.citations_resolve),
        degraded=sum(1 for r in rows if r.degraded),
        tokens_total=sum(r.tokens for r in rows),
        tokens_per_bundle=sum(r.tokens for r in rows) / n,
        wall_ms_mean=sum(r.wall_ms for r in rows) / n,
        accuracy_by_variant=acc_by_variant,
        accuracy_range=(max(acc_by_variant.values()) - min(acc_by_variant.values())
                        if acc_by_variant else 0.0),
        flip_rate=(flips / len(eligible)) if eligible else 0.0,
        attribution=dict(attribution),
    )


def summarize_all(rows: Sequence[ScoreRow]) -> dict[str, JudgeStats]:
    by_judge: dict[str, list[ScoreRow]] = defaultdict(list)
    for r in rows:
        by_judge[r.judge].append(r)
    return {j: summarize_judge(rs) for j, rs in sorted(by_judge.items())}


# --- the ambiguity check ------------------------------------------------------------------

def ambiguity_worked(rows: Sequence[ScoreRow], deterministic_judge: str = "B0") -> dict[str, Any]:
    """Did the fault set actually separate the judges, or did it test nothing?

    If B0 scores 100%, no agent can beat it and the comparison is uninformative regardless of
    what the other rows say. Measured on 2026-08-14 with the original four scenarios: exactly
    that happened. This turns the observation into something the report checks every run rather
    than something a reader has to notice.
    """
    b0 = [r for r in rows if r.judge == deterministic_judge]
    if not b0:
        return {"checked": False, "reason": f"no {deterministic_judge} rows"}
    acc = sum(1 for r in b0 if r.score == 1.0) / len(b0)
    headroom = [r.scenario for r in b0 if r.score != 1.0]
    return {
        "checked": True,
        "baseline_accuracy": acc,
        "informative": acc < 1.0,
        "scenarios_with_headroom": sorted(set(headroom)),
        "warning": ("" if acc < 1.0 else
                    f"{deterministic_judge} scores {acc:.0%}: no judge can beat it on this "
                    f"fault set, so any agent result is a tie at higher cost"),
    }
