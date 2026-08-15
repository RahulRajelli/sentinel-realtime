"""Every published figure must regenerate from the committed verdict files.

WHY THIS EXISTS.

On 2026-08-15 the v1->v2 schema migration recomputed every `bundle_id`. Verdicts store that id as
a foreign key, so all 60 committed verdict files stopped resolving and `e4_report.py` failed on
every one of them. Bundles migrated cleanly; the things POINTING at bundles did not. For a day
the repository's central claim -- that its numbers can be checked -- was false, and nothing
noticed. The suite was green the whole time, because no test read the verdict files.

That is the gap this closes. It is deliberately a test rather than a CI-only script, so it runs on
every `pytest -q` and fails on the machine that broke it, seconds after it breaks, instead of
being discovered later by someone running the reproduce command out of curiosity.

WHAT IT PINS, and why each one.

* Every verdict file resolves to a bundle. This is the exact failure above.
* B0 = 0.00 in every arm. B0 is deterministic code with no model in it. If it is not 0.00 the
  scenario is not ambiguous and no other number in that arm is readable, so this is a
  precondition rather than a result.
* B0 names a SYMPTOM as the root cause on all 3 bundles. This is the sharper statement: B0 does
  not merely score badly, it fails in the specific way the fault was constructed to produce. If
  this ever passes while B0 = 0.00 fails, something changed about the scenario, not the scoring.
* Each published accuracy equals the regenerated mean at the precision it was published to.
  The contract is "what is written on the page is what the files produce", so the comparison is
  made at 2 decimal places -- the form a reader actually sees.
* Five runs per arm. Rule 2 of this project is "never quote a single run"; three of its six
  retractions came from breaking it. A published mean silently computed over fewer runs is the
  same error wearing the same disguise.

MAINTAINING IT. If a figure legitimately changes, change PUBLISHED here in the same commit that
changes the write-ups, and the diff will show both. That coupling is the point: it is not
possible to quietly move a number on a page without this file objecting.
"""

from __future__ import annotations

import collections
import glob
import json
from pathlib import Path

import pytest

from sentinel.bundle import load_all
from sentinel.judges import Verdict
from sentinel.score import score_all

_ROOT = Path(__file__).resolve().parent.parent

# The published table, exactly as it appears in WHITEPAPER.md and docs/whitepaper*.html.
# `acc` is the 5-run mean at published precision; `sym` is the per-run count of judgements naming
# a symptom as the root cause, out of 9.
PUBLISHED = {
    "gpt-5.6-sol untimed": {
        "glob": "results/crossmodel/gpt2_run*.json",
        "acc": {"B0": 0.00, "B1": 0.71, "B3": 0.96},
        "sym": {"B0": 3, "B1": 0, "B3": 0},
    },
    "gpt-5.6-sol timed": {
        "glob": "results/crossmodel/gpt_timed*.json",
        "acc": {"B3": 1.00},
        "sym": {"B3": 0},
    },
    "gemini-2.5-flash timed": {
        "glob": "results/isolation/iso_timed*.json",
        "acc": {"B0": 0.00, "B3": 0.11},
        "sym": {"B0": 3, "B3": 8},
    },
    "gemini-2.5-flash untimed": {
        "glob": "results/isolation/iso_untimed*.json",
        "acc": {"B0": 0.00, "B3": 0.67},
        "sym": {"B0": 3, "B3": 1},
    },
    "gemini-2.5-flash variance": {
        "glob": "variance/var_run*.json",
        "acc": {"B0": 0.00, "B1": 0.91, "B3": 0.69},
        "sym": {"B0": 3, "B1": 0, "B3": 1},
    },
}

EXPECTED_RUNS = 5

# The flights every published figure is a mean over, pinned BY FILENAME.
#
# This was `only=["compass_offset"]` with a bare `len(b) == 3`, which counted whatever
# compass_offset bundles happened to be sitting in `bundles/`. That coupled "which flights the
# figures were computed over" to "which flights exist on disk" -- two different things. Capturing
# a fourth compass flight on 2026-08-15 turned 12 tests red and errored 26 more without a single
# published number having changed. That is the wrong direction for a gate to fail in: the fourth
# flight is exactly what the project needs (at n=3/arm the best possible two-sided Fisher exact p
# is 0.100, so the design could not produce a significant result at ANY effect size; n=4 takes the
# ceiling to 0.029), and a suite that goes red when you collect evidence teaches you not to
# collect it.
#
# Pinning names is also STRICTLY STRONGER than counting them. A count of 3 still passes if one
# bundle is swapped for a different one; this does not. Adding a flight to a published mean now
# requires editing this list in the same commit as the figures -- which is the coupling the
# module docstring asks for, enforced rather than requested.
PUBLISHED_FLIGHTS = [
    "compass_offset_r0.json",
    "compass_offset_r1.json",
    "compass_offset_r2.json",
]


@pytest.fixture(scope="module")
def bundles():
    b = load_all(str(_ROOT / "bundles"), only=PUBLISHED_FLIGHTS)
    assert len(b) == len(PUBLISHED_FLIGHTS), (
        f"expected the {len(PUBLISHED_FLIGHTS)} pinned compass_offset flights "
        f"{PUBLISHED_FLIGHTS}, loaded {len(b)}. Every published figure is a mean over exactly "
        f"those flights -- if one is renamed or removed, the published numbers no longer have a "
        f"source, and if a new flight belongs in the mean it goes in this list and the figures "
        f"are re-run in the same commit")
    return b


def _files(pattern: str) -> list[Path]:
    return sorted(Path(p) for p in glob.glob(str(_ROOT / pattern)))


def _verdicts(path: Path) -> list[Verdict]:
    raw = json.loads(path.read_text())
    vs = raw if isinstance(raw, list) else raw.get("verdicts", [])
    return [Verdict.model_validate(v) for v in vs]


_ARM_CACHE: dict[str, tuple] = {}


def _arm(bundles, pattern: str):
    """Scored arm, memoised.

    Six assertions x five arms re-scored the same files thirty times and took 84 s, against 2 s
    for the rest of the suite. A gate that slow stops being run, which is the one failure mode a
    gate cannot have -- so the scoring happens once per pattern and every assertion reads it.
    """
    if pattern not in _ARM_CACHE:
        _ARM_CACHE[pattern] = _score_arm(bundles, pattern)
    return _ARM_CACHE[pattern]


def _score_arm(bundles, pattern: str):
    """Return ({judge: [per-run means]}, {judge: [per-run symptom counts]}) for one arm."""
    acc = collections.defaultdict(list)
    sym = collections.defaultdict(list)
    for f in _files(pattern):
        rows = score_all(bundles, _verdicts(f))
        per = collections.defaultdict(lambda: collections.defaultdict(list))
        counts = collections.Counter()
        for r in rows:
            per[r.judge][r.bundle_id].append(r.score)
            counts[r.judge] += int(r.named_symptom_as_root)
        for judge, by_bundle in per.items():
            # Bundle-level, never judgement-level: 3 flights x 3 prompt variants is 9 judgements
            # but only 3 independent flights, and averaging over judgements inflates n threefold.
            means = [sum(v) / len(v) for v in by_bundle.values()]
            acc[judge].append(sum(means) / len(means))
            sym[judge].append(counts[judge])
    return acc, sym


@pytest.mark.parametrize("arm", sorted(PUBLISHED))
def test_every_verdict_file_resolves_to_a_bundle(bundles, arm):
    """The 2026-08-15 regression: bundle_id is a foreign key and the migration moved it."""
    files = _files(PUBLISHED[arm]["glob"])
    assert files, f"{arm}: no verdict files matched {PUBLISHED[arm]['glob']}"
    for f in files:
        # score_all raises "verdict cites unknown bundle" when the join breaks.
        score_all(bundles, _verdicts(f))


@pytest.mark.parametrize("arm", sorted(PUBLISHED))
def test_arm_has_five_runs(arm):
    """Never quote a single run. Three of this project's six retractions came from doing so."""
    files = _files(PUBLISHED[arm]["glob"])
    assert len(files) == EXPECTED_RUNS, (
        f"{arm}: {len(files)} run files, expected {EXPECTED_RUNS}. A published mean over a "
        f"different number of runs is not the number that was published")


@pytest.mark.parametrize("arm", sorted(PUBLISHED))
def test_b0_is_exactly_zero(bundles, arm):
    """Precondition, not a result. B0 is deterministic code with no model in it."""
    acc, _ = _arm(bundles, PUBLISHED[arm]["glob"])
    if "B0" not in acc:
        pytest.skip(f"{arm} did not run B0")
    for i, value in enumerate(acc["B0"], start=1):
        assert value == pytest.approx(0.0, abs=1e-9), (
            f"{arm} run{i}: B0 scored {value}, not 0.00. The scenario is no longer ambiguous, "
            f"so nothing else in this arm is readable")


@pytest.mark.parametrize("arm", sorted(PUBLISHED))
def test_b0_fails_in_the_constructed_way(bundles, arm):
    """B0 does not merely score 0.00 -- it names the symptom, on every bundle.

    That is the construction: the persistence gate in compass.py puts the cause 1.0 s after its
    own symptom, and B0's rule is "first advisory after injection is the root cause". If this
    stops holding, the fault has changed, whatever the accuracy column says.
    """
    _, sym = _arm(bundles, PUBLISHED[arm]["glob"])
    if "B0" not in sym:
        pytest.skip(f"{arm} did not run B0")
    for i, count in enumerate(sym["B0"], start=1):
        assert count == len(PUBLISHED_FLIGHTS), (
            f"{arm} run{i}: B0 named a symptom as root {count} times, expected "
            f"{len(PUBLISHED_FLIGHTS)} (once per pinned flight)")


@pytest.mark.parametrize("arm", sorted(PUBLISHED))
def test_published_accuracy_regenerates(bundles, arm):
    """What the page says equals what the files produce, at the precision the page says it."""
    acc, _ = _arm(bundles, PUBLISHED[arm]["glob"])
    for judge, expected in PUBLISHED[arm]["acc"].items():
        assert judge in acc, f"{arm}: no {judge} verdicts found, but {expected} is published"
        got = sum(acc[judge]) / len(acc[judge])
        assert round(got, 2) == pytest.approx(expected, abs=1e-9), (
            f"{arm} {judge}: published {expected:.2f}, regenerates to {got:.4f} "
            f"({round(got, 2):.2f}). Either the write-ups or this table is now wrong")


@pytest.mark.parametrize("arm", sorted(PUBLISHED))
def test_published_symptom_counts_regenerate(bundles, arm):
    """The trap metric. 8/9 vs 0/9 is the whole cross-model finding, so it is pinned too."""
    _, sym = _arm(bundles, PUBLISHED[arm]["glob"])
    for judge, expected in PUBLISHED[arm]["sym"].items():
        assert judge in sym, f"{arm}: no {judge} verdicts found"
        for i, count in enumerate(sym[judge], start=1):
            assert count == expected, (
                f"{arm} {judge} run{i}: named a symptom as root {count}/9, published {expected}/9")


def test_the_inversion_still_inverts(bundles):
    """The headline claim, stated as an ordering rather than as two numbers.

    Numbers drift with scoring changes; the FINDING is that the ranking of B1 and B3 reverses
    between model families. If a future change leaves both means plausible but stops them
    crossing, every write-up built on this is wrong and the accuracy assertions alone would not
    say so.
    """
    gpt_acc, _ = _arm(bundles, PUBLISHED["gpt-5.6-sol untimed"]["glob"])
    gem_acc, _ = _arm(bundles, PUBLISHED["gemini-2.5-flash variance"]["glob"])

    gpt_b1 = sum(gpt_acc["B1"]) / len(gpt_acc["B1"])
    gpt_b3 = sum(gpt_acc["B3"]) / len(gpt_acc["B3"])
    gem_b1 = sum(gem_acc["B1"]) / len(gem_acc["B1"])
    gem_b3 = sum(gem_acc["B3"]) / len(gem_acc["B3"])

    assert gpt_b3 > gpt_b1, f"gpt: agent {gpt_b3:.3f} no longer beats single-shot {gpt_b1:.3f}"
    assert gem_b3 < gem_b1, f"gemini: agent {gem_b3:.3f} no longer loses to single-shot {gem_b1:.3f}"
