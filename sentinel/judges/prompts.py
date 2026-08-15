"""Prompt variants (Phase E4).

Three paraphrases of one task. *Prompt-Induced Waste* found that wording changes redirect agent
effort without improving success rates, so the spread across these is a **published number**, not
a knob to tune until the agent looks good. Picking the best-performing variant and reporting only
that is the specific dishonesty this file exists to make impossible.

The three deliberately span different styles -- terse imperative, role-framed, and explicit
checklist -- because those are the styles that differ in the literature. What they do NOT differ
in is the task definition or the output contract: `TASK_RULES` and `OUTPUT_CONTRACT` are shared
verbatim. If the schema varied between variants, the measured variance would be schema
sensitivity wearing a paraphrase costume.

Nothing here names a fault type, a scenario, or how many faults exist. A prompt that listed the
candidate answers would be leaking the label more subtly than a field called
`expected_root_cause`, and would be harder to notice.
"""

from __future__ import annotations

TASK_RULES = """\
One fault occurred, or none at all. A single fault usually trips several detectors: the one that
is detected FIRST is not necessarily the one that CAUSED the others.

Your job is to name the root cause -- the fault that explains the rest -- not the loudest or
earliest symptom. If the evidence shows nothing actually went wrong, say so; a clean flight is a
valid and expected answer, and inventing a fault for one is worse than staying silent.

Every claim must be supported by something you actually observed through the tools. Cite the
metric name, and anchor it to EITHER the timestamp you saw it at OR the measured value you saw --
whichever your tools actually gave you. Some tools deliberately report no timestamps; when you
use those, cite the value. Do not cite a metric you did not look up, do not cite a time outside
the flight window, and do not cite a value you did not observe. A citation with neither a time
nor a value points at nothing and does not count."""

OUTPUT_CONTRACT = """\
Reply with a single JSON object and nothing else:

{
  "root_cause": "<incident_type>" or null,
  "symptoms":  ["<incident_type>", ...],
  "confidence": <number between 0 and 1>,
  "rationale": "<two sentences at most>",
  "citations": [{"metric": "<name>", "t": <seconds or null>, "value": <number or null>}]
}

`root_cause` must be an incident type that this flight actually detected, or null.
`citations` must contain at least one entry whenever `root_cause` is not null, and each entry
must carry a `t`, a `value`, or both -- at least one of the two, never neither."""

_V1 = """\
Identify the root cause of this flight's fault.

{rules}

{contract}"""

_V2 = """\
You are a UAV reliability engineer reviewing a flight after an anomaly was reported. A colleague
will act on your answer, so be precise about which fault is the cause and which are consequences.

{rules}

{contract}"""

_V3 = """\
Work through this diagnosis in order:

  1. List every advisory the system raised.
  2. Establish which incident types were detected first, and by how much, using the ordering tool.
  3. Look up the evidence behind the candidates -- measured values against their thresholds.
  4. Decide which single fault explains the others, rather than which one arrived first.
  5. Answer, citing what you actually looked up.

{rules}

{contract}"""

VARIANTS: dict[str, str] = {
    "v1": _V1.format(rules=TASK_RULES, contract=OUTPUT_CONTRACT),
    "v2": _V2.format(rules=TASK_RULES, contract=OUTPUT_CONTRACT),
    "v3": _V3.format(rules=TASK_RULES, contract=OUTPUT_CONTRACT),
}


def system_prompt(variant: str) -> str:
    if variant not in VARIANTS:
        raise KeyError(f"unknown prompt variant {variant!r}; have {sorted(VARIANTS)}")
    return VARIANTS[variant]
