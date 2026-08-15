# START HERE — for a fresh agent picking this up

You are continuing work on **SentinelAgent Realtime**: a drone flight-anomaly detector plus an
evaluation harness that measures whether an LLM judge can name the ROOT CAUSE of a fault rather
than the first symptom that fired.

`HANDOFF.md` is the full technical state. This file is the 5-minute version and the one job.

---

## 1. Paths and environment

```
repo            D:\Rahul website\sentinel-realtime
sibling package D:\Rahul website\ardupilot-log-analyzer   (flightdx — the detectors)
python          ..\ardupilot-log-analyzer\.venv\Scripts\python.exe
```

The system Python has no deps. **Always use that venv.** Verify in one command:

```bash
../ardupilot-log-analyzer/.venv/Scripts/python.exe -m pytest -q     # expect 110 passed, ~2s
```

All 110 tests are offline — no simulator, no network, no API key. If they pass, the checkout is
healthy and you can work without touching hardware.

SITL (the flight simulator) lives in WSL and is **only** needed to capture new flights. You almost
certainly do not need it:

```bash
wsl -d Ubuntu-24.04 -- bash -c "ls /root/ardupilot/build/sitl/bin/"
```

---

## 2. The one job

**Run the judge comparison on a NON-Gemini model.**

Every result in this project was measured on `gemini-2.5-flash`. The headline finding may be a
property of that one model rather than of LLM agents in general, and nobody knows which. That is
the single highest-value open question, and it needs no simulator and no new code.

```bash
export OPENAI_API_KEY=...        # any OpenAI-compatible provider

../ardupilot-log-analyzer/.venv/Scripts/python.exe scripts/e4_judge.py \
  --bundles bundles --only compass_offset \
  --judges B0,B1,B3 \
  --provider openai --model gpt-5.6 \
  --out verdicts_gpt_run1.json

../ardupilot-log-analyzer/.venv/Scripts/python.exe scripts/e4_report.py \
  --bundles bundles --verdicts verdicts_gpt_run1.json --only compass_offset
```

Other providers — only `--base-url` changes:

| provider | flags |
|---|---|
| OpenAI | *(omit `--base-url`)* |
| Grok | `--model grok-4.6 --base-url https://api.x.ai/v1` |
| OpenRouter | `--base-url https://openrouter.ai/api/v1` |
| local (Ollama/vLLM) | `--base-url http://localhost:11434/v1`, `OPENAI_API_KEY=none` |

**Run it FIVE times and report a mean and spread.** This is not optional. The models are not
deterministic even at `temperature=0`; measured spread is 0.11 (one judgement in nine), and a
single run already produced one wrong published claim here. Change only the `--out` filename
between runs.

### What the answer means

On `compass_offset`, measured on gemini-2.5-flash over five runs:

| judge | what it is | mean |
|---|---|---|
| B0 | deterministic rule: "first advisory after injection is the root cause" | **0.00** |
| B1 | one LLM call, no tools | **0.91** |
| B3 | tool-using agent | **0.69** |

* **If your model reproduces roughly this shape** — B1 > B3, B0 at zero — the finding is about
  agents, and that is publishable.
* **If B3 matches or beats B1** — the finding was gemini-specific. Equally valuable, and it means
  the current write-up must be narrowed. Report it; do not bury it.

B0 must come out at exactly **0.00** every time. It is deterministic code with no model in it.
If it does not, something is wrong with the harness and nothing else in the run can be trusted.

---

## 3. Rules that are not style preferences

1. **Name the model in anything you publish.** Every table must say which model produced it. This
   project's entire argument is that its numbers can be checked; "an LLM agent" is unfalsifiable.
2. **Never quote a single run.** Five repeats, mean and spread.
3. **Quote the BUNDLE-level interval** (`ci_bundle`), not the judgement-level one. 9 bundles x 3
   prompt variants is 27 judgements but only 9 independent flights — treating them as 27 inflates
   n threefold. This exact error was made here and had to be retracted.
4. **Never lower a detector threshold to make a scenario pass.** That makes the experiment succeed
   by redefining the fault. If a fault cannot be produced, record why and move on — three already
   have been (see HANDOFF section 2).
5. **Console output must stay ASCII.** Windows cp1252 renders anything else as `?`.
6. Use `-u` on backgrounded Python or the log stays empty and looks hung.

---

## 4. Known-open, in value order

1. **`evidence_untimed` verdicts cannot carry a citation.** Every run has ~2 of 9 agent verdicts
   naming the CORRECT root cause and scoring 0 because they have no citation — `Citation` requires
   a timestamp that resolves inside the flight window, and that tool removes all timestamps. This
   single defect is the entire remaining B3-vs-B1 gap. Three candidate fixes and their tradeoffs
   are in HANDOFF section 4. **This is a design decision — ask the owner, do not just pick one.**
2. **Only one scenario discriminates.** `compass_offset` is the sole fault where the judges differ;
   everything else scores 1.00 for everyone. A second mechanism probably needs a new *detector*,
   not a new scenario — three candidates are already ruled out with measurements.
3. **`replay_2024-04-30 17-30-57.json` will not load.** It needs its original `.BIN`, which is not
   in the repo. It is the only evidence behind the 96.8% suppression claim, so that number cannot
   be published until it is regenerated. Ask the owner for the log.

---

## 5. Do not re-litigate

These were decided on measurement and the reasoning is in the code comments:

* capture and judgement are separated (fly once, freeze a `RunBundle`, judge offline);
* `bundle_id` excludes wall-clock timings;
* transport failures degrade to the deterministic baseline and are attributed to HARNESS, never
  to the model;
* replay sets no ground-truth label;
* no RAG, no vector DB, and no LLM anywhere near flight control.

Changing `_identity_payload` or `_TIMING_FIELDS` **must** bump `SCHEMA_VERSION` in the same commit,
or every existing bundle silently fails to load and reports itself as tampered with.
