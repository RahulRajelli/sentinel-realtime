"""Rubric grader -- the SECOND rater, and nothing else (Phase E4).

This exists only so Cohen's kappa has two opinions to compare. `score.py` remains the authority:
when the grader and the code scorer disagree, the code is right and the disagreement is the
measurement. *Reliability without Validity* found a 33-41 point judge validation gap that raw
agreement hides, so the point of this file is to find out whether LLM grading is trustworthy on
this task -- not to grade anything that matters.

**A low kappa is a result, not a bug.** If the grader disagrees with deterministic scoring, the
finding is "LLM grading is unreliable here, kappa = x" and every number in the table still stands
because none of them came from this file.

The grader sees the ground truth on purpose -- it is scoring an already-produced verdict, not
producing one. That is the opposite of the judges, from whom the label is unreachable by
construction (`tools.py`).
"""

from __future__ import annotations

import json
from typing import Any

from sentinel.bundle import RunBundle
from sentinel.judges import Verdict
from sentinel.judges.model import ModelClient

GRADE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "correct": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["correct", "reason"],
    "additionalProperties": False,
}

RUBRIC = """\
You are grading one fault diagnosis against a known answer. Mark it correct only if the
diagnosis names the same root cause as the known answer.

Rules, applied strictly:
  - Naming a symptom of the fault instead of the fault itself is INCORRECT, even though the
    symptom was genuinely observed.
  - "No fault" is the correct answer when the known answer is no fault, and incorrect otherwise.
  - Judge only the root cause. Do not reward a good explanation attached to the wrong answer,
    and do not penalise a terse one attached to the right answer.

Reply with a single JSON object: {"correct": <bool>, "reason": "<one sentence>"}"""


class RubricGrader:
    """Grades a verdict against the bundle's ground truth. Never used to produce a verdict."""

    id = "grader"

    def __init__(self, client: ModelClient) -> None:
        self.client = client

    def grade(self, bundle: RunBundle, verdict: Verdict) -> bool | None:
        """True/False, or None when the grader could not be read.

        None is propagated rather than defaulted: guessing a grade to keep the sample size up
        would corrupt the very agreement statistic this file exists to measure.
        """
        payload = {
            "known_root_cause": bundle.expected_root_cause,
            "known_symptoms_of_that_fault": bundle.expected_symptoms,
            "diagnosis_under_review": {
                "root_cause": verdict.root_cause,
                "symptoms": verdict.symptoms,
                "rationale": verdict.rationale,
            },
        }
        resp = self.client.complete(
            messages=[{"role": "system", "content": RUBRIC},
                      {"role": "user", "content": json.dumps(payload, default=str)}],
            tools=None,
        )
        if not resp.text:
            return None
        try:
            out = json.loads(resp.text)
        except (ValueError, TypeError):
            from sentinel.judges.agent import AgentJudge
            out = AgentJudge._parse(resp.text)
        if not isinstance(out, dict) or "correct" not in out:
            return None
        return bool(out["correct"])


def label_pairs(bundles: list[RunBundle], verdicts: list[Verdict], rows,
                grader: RubricGrader) -> list[tuple[str, str]]:
    """Paired (code_scorer, llm_grader) labels for `stats.kappa_with_ci`.

    Items the grader could not read are dropped from BOTH raters rather than filled in, so the
    pairing stays honest and the dropped count is visible as a smaller n.
    """
    by_id = {b.bundle_id: b for b in bundles}
    by_key = {(v.judge, v.prompt_variant, v.bundle_id): v for v in verdicts}
    pairs: list[tuple[str, str]] = []

    for row in rows:
        verdict = by_key.get((row.judge, row.variant, row.bundle_id))
        bundle = by_id.get(row.bundle_id)
        if verdict is None or bundle is None:
            continue
        grade = grader.grade(bundle, verdict)
        if grade is None:
            continue
        pairs.append(("correct" if row.score == 1.0 else "incorrect",
                      "correct" if grade else "incorrect"))
    return pairs
