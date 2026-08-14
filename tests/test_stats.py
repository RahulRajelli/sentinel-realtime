"""Step 8 acceptance: intervals, kappa, prompt sensitivity, and the ambiguity check.

Wilson and kappa are checked against values computed by hand rather than against whatever this
implementation happens to produce, so a regression in the formula fails instead of redefining
the expected answer.
"""

from __future__ import annotations

import math

import pytest

from sentinel.score import ScoreRow
from sentinel.stats import (
    ambiguity_worked,
    bootstrap_ci,
    cohen_kappa,
    kappa_with_ci,
    summarize_all,
    summarize_judge,
    wilson_interval,
)


def row(judge="B3", variant="v1", bundle="b1", scenario="compass_offset",
        score=1.0, predicted="compass_inconsistency", **kw) -> ScoreRow:
    return ScoreRow(bundle_id=bundle, scenario=scenario, judge=judge, variant=variant,
                    expected="compass_inconsistency", predicted=predicted, score=score,
                    correct=(score == 1.0), **kw)


# --- Wilson --------------------------------------------------------------------------------

def test_wilson_stays_inside_zero_one_at_perfect_scores():
    """The reason Wilson is used instead of the normal approximation: 12/12 must not exceed 1."""
    lo, hi = wilson_interval(12, 12)
    assert 0.0 <= lo <= hi <= 1.0
    assert hi == 1.0 and lo < 1.0, "a perfect score is not proof of a perfect judge"


def test_wilson_matches_hand_computed_values():
    lo, hi = wilson_interval(4, 4)          # the current fault set: B0 goes 4/4
    assert lo == pytest.approx(0.5101, abs=1e-3)
    assert hi == pytest.approx(1.0, abs=1e-6)
    lo, hi = wilson_interval(5, 10)
    assert lo == pytest.approx(0.2366, abs=1e-3)
    assert hi == pytest.approx(0.7634, abs=1e-3)


def test_wilson_is_wide_at_n_equals_four_and_narrower_at_twelve():
    """Quantifies why the plan recommends --repeat 3."""
    w4 = wilson_interval(4, 4)
    w12 = wilson_interval(12, 12)
    assert (w4[1] - w4[0]) > (w12[1] - w12[0])
    assert (w4[1] - w4[0]) > 0.45, "n=4 cannot support a point estimate"


def test_wilson_with_no_data_returns_the_whole_interval():
    assert wilson_interval(0, 0) == (0.0, 1.0)


# --- kappa ---------------------------------------------------------------------------------

def test_kappa_matches_a_hand_computed_case():
    a = ["y", "y", "n", "n", "y", "n"]
    b = ["y", "n", "n", "n", "y", "y"]
    # po = 4/6; pe = (3/6)(3/6) + (3/6)(3/6) = 0.5; kappa = (0.6667-0.5)/0.5
    assert cohen_kappa(a, b) == pytest.approx(0.3333, abs=1e-4)


def test_perfect_agreement_with_label_variation_is_one():
    assert cohen_kappa(["y", "n", "y"], ["y", "n", "y"]) == pytest.approx(1.0)


def test_kappa_is_undefined_when_neither_rater_varies():
    """The kappa paradox, and the case most likely to occur here: B0 already scores 4/4, so
    both raters may label everything 'correct'. NaN is the honest answer -- 1.0 would be a
    fabricated result and 0.0 a fabricated failure."""
    assert math.isnan(cohen_kappa(["correct"] * 8, ["correct"] * 8))


def test_kappa_can_be_negative_when_raters_disagree_worse_than_chance():
    assert cohen_kappa(["y", "y", "n", "n"], ["n", "n", "y", "y"]) < 0


def test_kappa_refuses_mismatched_rater_lengths():
    with pytest.raises(ValueError, match="item count"):
        cohen_kappa(["y"], ["y", "n"])


def test_kappa_ci_is_deterministic_and_brackets_the_estimate():
    pairs = [("y", "y"), ("y", "n"), ("n", "n"), ("n", "n"),
             ("y", "y"), ("n", "y"), ("y", "y"), ("n", "n")]
    k1, lo1, hi1 = kappa_with_ci(pairs, n_boot=500, seed=0)
    k2, lo2, hi2 = kappa_with_ci(pairs, n_boot=500, seed=0)
    assert (k1, lo1, hi1) == (k2, lo2, hi2), "a report that moved between runs is not reproducible"
    assert lo1 <= k1 <= hi1


def test_kappa_ci_is_undefined_when_too_few_replicates_survive():
    """All-agreeing data leaves kappa undefined in most resamples; the CI must say so rather
    than compute a number from the handful that survived."""
    _, lo, hi = kappa_with_ci([("c", "c")] * 10, n_boot=300, seed=0)
    assert math.isnan(lo) and math.isnan(hi)


def test_bootstrap_on_empty_input_is_nan_not_a_crash():
    lo, hi = bootstrap_ci([], lambda s: 1.0)
    assert math.isnan(lo) and math.isnan(hi)


# --- per-judge aggregation -------------------------------------------------------------------

def test_flip_rate_counts_answers_that_changed_with_wording_only():
    rows = [
        row(bundle="b1", variant="v1", predicted="compass_inconsistency", score=1.0),
        row(bundle="b1", variant="v2", predicted="ekf_inconsistency", score=0.0),   # flipped
        row(bundle="b1", variant="v3", predicted="compass_inconsistency", score=1.0),
        row(bundle="b2", variant="v1", predicted="compass_inconsistency", score=1.0),
        row(bundle="b2", variant="v2", predicted="compass_inconsistency", score=1.0),
        row(bundle="b2", variant="v3", predicted="compass_inconsistency", score=1.0),
    ]
    s = summarize_judge(rows)
    assert s.flip_rate == pytest.approx(0.5)          # b1 flipped, b2 did not
    # v1 = 2/2, v2 = 1/2, v3 = 2/2 -> the spread wording alone is worth
    assert s.accuracy_by_variant == {"v1": 1.0, "v2": 0.5, "v3": 1.0}
    assert s.accuracy_range == pytest.approx(0.5)


def test_a_judge_can_hold_accuracy_steady_while_flipping_every_answer():
    """Accuracy alone would call this judge stable. It is not."""
    rows = [
        row(bundle="b1", variant="v1", predicted="compass_inconsistency", score=1.0),
        row(bundle="b1", variant="v2", predicted="ekf_inconsistency", score=0.0),
        row(bundle="b2", variant="v1", predicted="ekf_inconsistency", score=0.0),
        row(bundle="b2", variant="v2", predicted="compass_inconsistency", score=1.0),
    ]
    s = summarize_judge(rows)
    assert s.accuracy_by_variant == {"v1": 0.5, "v2": 0.5}
    assert s.accuracy_range == 0.0
    assert s.flip_rate == 1.0


def test_single_variant_judge_is_not_credited_with_stability():
    """B0 has one variant by construction and must not score flip_rate 0 for that reason."""
    s = summarize_judge([row(judge="B0", variant="-", bundle="b1"),
                         row(judge="B0", variant="-", bundle="b2")])
    assert s.flip_rate == 0.0 and s.accuracy_range == 0.0
    assert s.accuracy_by_variant == {"-": 1.0}


def test_cost_and_failure_counts_aggregate():
    rows = [
        row(score=0.0, predicted="ekf_inconsistency", named_symptom_as_root=True,
            tokens=1200, wall_ms=800.0, attribution="model"),
        row(score=0.0, degraded=True, tokens=800, wall_ms=400.0, attribution="harness"),
        row(score=1.0, tokens=1000, wall_ms=600.0),
    ]
    s = summarize_judge(rows)
    assert s.named_symptom_as_root == 1 and s.degraded == 1
    assert s.tokens_per_bundle == pytest.approx(1000.0)
    assert s.attribution == {"model": 1, "harness": 1}


def test_mixed_judges_refuse_to_aggregate():
    with pytest.raises(ValueError, match="mixed judges"):
        summarize_judge([row(judge="B0"), row(judge="B3")])


def test_summarize_all_splits_by_judge():
    out = summarize_all([row(judge="B0", variant="-"), row(judge="B3", score=0.0)])
    assert set(out) == {"B0", "B3"} and out["B3"].accuracy == 0.0


# --- the ambiguity check -----------------------------------------------------------------------

def test_a_fault_set_the_baseline_aces_is_flagged_uninformative():
    """The 2026-08-14 finding, enforced on every future run."""
    check = ambiguity_worked([row(judge="B0", variant="-", bundle=f"b{i}") for i in range(4)])
    assert check["informative"] is False
    assert "no judge can beat it" in check["warning"]


def test_a_fault_set_with_headroom_passes():
    rows = [row(judge="B0", variant="-", bundle="b1", scenario="compass_offset",
                score=0.0, predicted="ekf_inconsistency"),
            row(judge="B0", variant="-", bundle="b2", scenario="null", score=1.0)]
    check = ambiguity_worked(rows)
    assert check["informative"] is True
    assert check["scenarios_with_headroom"] == ["compass_offset"]
    assert check["warning"] == ""
