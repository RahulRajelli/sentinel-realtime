"""B0 -- the deterministic baseline (Phase E4).

The rule: **the first advisory the escalation gate raised after injection is the root cause.**
That is not a strawman invented to lose. It is what `sentinel/gate.py` already does in
production, it costs zero tokens, and on a fault whose own detector is the fastest to fire it is
exactly right.

This is the number every other judge has to beat. FOCUS.md's honesty rule applies here more than
anywhere: if B0 wins, that is the finding, and it gets published.

B0 also serves as the fallback answer when a spend ceiling trips (`budget.py`, step 3), which is
why it is deliberately free of any failure mode of its own -- it cannot time out, cannot
hallucinate a fault type that never fired, and cannot cite a timestamp outside the window.

What B0 structurally cannot do, and the reason E4 exists: when a fault's *symptom* is detected
before its *cause* -- a stiff airframe clipping the accelerometer before the vibration detector
has enough window to call it, wind read as actuator asymmetry -- "first" and "root" come apart,
and B0 answers the symptom with total confidence.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from sentinel.bundle import RunBundle
from sentinel.judges import Citation, Verdict

if TYPE_CHECKING:
    from sentinel.budget import Budget


class DeterministicJudge:
    id = "B0"

    def judge(self, bundle: RunBundle, budget: "Budget | None" = None,
              variant: str = "-") -> Verdict:
        t0 = time.perf_counter()

        # Pre-injection advisories are false positives, not candidates. The null scenario never
        # injects at all, so `pre_inject` is read from the record rather than recomputed from a
        # comparison against a t_inject that may not exist.
        post = [a for a in bundle.advisories if not a.pre_inject]
        post.sort(key=lambda a: a.t)

        root = post[0].type if post else None
        # Everything else raised in the same flight. Reported, never used to pick the root --
        # gate.py:21 refuses to pick a winner and B0 inherits that refusal; it takes the first,
        # which is a rule, not a judgement.
        symptoms = [t for t in dict.fromkeys(a.type for a in post) if t != root]

        citations = self._cite(bundle, root, post[0].t if post else None)

        return Verdict(
            judge=self.id,
            bundle_id=bundle.bundle_id,
            prompt_variant="-",
            root_cause=root,
            symptoms=symptoms,
            confidence=None,          # B0 has no calibrated notion of confidence and does not fake one
            rationale=("first advisory raised after injection"
                       if root else "no advisory raised after injection"),
            citations=citations,
            tokens_in=0, tokens_out=0, calls=0,
            wall_ms=(time.perf_counter() - t0) * 1000.0,
        )

    @staticmethod
    def _cite(bundle: RunBundle, root: str | None, t_adv: float | None) -> list[Citation]:
        """Cite the detector evidence behind the chosen advisory.

        The advisory record carries only type/severity, so the evidence is recovered from the
        incident that produced it: the first `Incident` of that type at or before the advisory
        time. Scored verdicts must cite something that resolves inside the window, and B0 must
        never be the judge that fails that check -- it is the fallback everything else degrades to.
        """
        if root is None or t_adv is None:
            return []

        for cycle in bundle.cycles:
            if cycle.t > t_adv:
                break
            for inc in cycle.incidents:
                if inc.type != root:
                    continue
                if inc.evidence:
                    ev = inc.evidence[0]
                    return [Citation(metric=ev.metric, t=cycle.t, value=ev.value)]
                # A detector that emitted no Evidence still gives a citable type+time.
                return [Citation(metric=inc.type, t=cycle.t)]

        return [Citation(metric=root, t=t_adv)]
