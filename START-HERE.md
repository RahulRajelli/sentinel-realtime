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
../ardupilot-log-analyzer/.venv/Scripts/python.exe -m pytest -q     # expect 118 passed, ~2s
```

All 118 tests are offline — no simulator, no network, no API key. If they pass, the checkout is
healthy and you can work without touching hardware.

SITL (the flight simulator) lives in WSL and is **only** needed to capture new flights. You almost
certainly do not need it:

```bash
wsl -d Ubuntu-24.04 -- bash -c "ls /root/ardupilot/build/sitl/bin/"
```

---

## 2. The one job

**Find a SECOND discriminating fault scenario.**

Everything measured here rests on ONE fault, `compass_offset`, which is 3 flights. Every other
scenario scores 1.00 for every judge and separates nothing. Adding models does not fix this —
two have already been run and they disagree with each other (see HANDOFF section 1).

A discriminating scenario is one where the SYMPTOM is detected before the CAUSE, so that the
deterministic baseline B0 — whose rule is literally "the first advisory after injection is the
root cause" — is wrong by construction. `compass_offset` achieves that because
`compass.py:45 MIN_ANOMALY_S = 1.0` forces the compass detector to wait a second while `ekf.py`
fires immediately.

**Read HANDOFF section 4 before designing one.** Three candidates are already ruled out with
measurements, and `compass.py` / `oscillation.py` hold the only two time gates in the whole
detector set — one of which is unreachable. A new pair most likely needs a new DETECTOR with its
own persistence gate, not a new scenario file.

**Never make a scenario work by lowering a detector threshold.** That makes the experiment pass
by redefining the fault, which is the one move this project cannot make.

### Verifying anything you build

Judge it on two models and five runs each — never one of either:

```bash
export OPENAI_BASE_URL="https://api.llmapi.ai/v1"
export OPENAI_API_KEY=...     # opencode stores it under `llmapi` in auth.json

PY=../ardupilot-log-analyzer/.venv/Scripts/python.exe

$PY scripts/e4_judge.py --bundles bundles --only YOUR_SCENARIO     --judges B0,B1,B3 --provider openai --model gpt-5.6-sol --out verdicts_x1.json

$PY scripts/e4_report.py --bundles bundles --verdicts verdicts_x1.json --only YOUR_SCENARIO
```

Swap `--model` for `claude-opus-5`, `qwen3.8-max`, `kimi-k3` — ~389 models on that gateway. Or
`--provider gemini` for Gemini via Google ADC. **No Anthropic key is needed or wanted.**

**B0 must score 0.00 on a good ambiguous scenario, on every model, every run.** It is
deterministic code with no model in it. If B0 is not 0.00, the scenario is not ambiguous and
nothing else in the run means anything.

## 3. Rules that are not style preferences

1. **Name the model in anything you publish.** Every table must say which model produced it. This
   project's entire argument is that its numbers can be checked; "an LLM agent" is unfalsifiable.
   Measured 2026-08-15: gemini-2.5-flash and gpt-5.6-sol rank the judges in OPPOSITE order on the
   same bundles. A finding without a model name attached is not just vague, it may be false.
2. **Never quote a single run.** Five repeats, mean and spread. Spread is 0.11 — one judgement
   in nine — and single runs produced two retracted claims here.
3. **Never edit code while an experiment is running.** It crashed a sweep mid-run and left five
   runs straddling a scoring change; the whole set was discarded.
4. **Quote the BUNDLE-level interval** (`ci_bundle`), not the judgement-level one. 9 bundles x 3
   prompt variants is 27 judgements but only 9 independent flights — treating them as 27 inflates
   n threefold. This exact error was made here and had to be retracted.
5. **Never lower a detector threshold to make a scenario pass.** That makes the experiment succeed
   by redefining the fault. If a fault cannot be produced, record why and move on — three already
   have been (see HANDOFF section 2).
6. **Console output must stay ASCII.** Windows cp1252 renders anything else as `?`.
7. Use `-u` on backgrounded Python or the log stays empty and looks hung.

---

## 4. Known-open, in value order

1. **Which tool surface should be the DEFAULT is undecided.** `SPECS` was set to the
   timestamp-free surface on gemini-only evidence. Measured since: it rescues gemini
   (0.11 -> 0.67) and very slightly costs gpt-5.6-sol (1.00 -> 0.96). **Do not flip it again
   without measuring both models.** HANDOFF section 1 has the full 2x2.
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
