# Quickstart — five minutes, no hardware

**You do not need a drone, a simulator, an AI account, or an API key to check that this works.**
The flights are already captured and committed, so the whole thing runs offline on your machine.

If any step does not print roughly what is shown below, that is a bug and worth an issue.

---

## 1. Install (two clones, one reason)

The detectors live in a sibling package, `flightdx`, which is not on PyPI. So it is two clones
rather than one. They must sit **next to each other**.

```bash
git clone https://github.com/RahulRajelli/sentinel-realtime
git clone https://github.com/RahulRajelli/ardupilot-log-analyzer
cd sentinel-realtime
```

```bash
python -m venv .venv
source .venv/bin/activate        # Linux / macOS
# .venv\Scripts\activate         # Windows
```

```bash
pip install -e .
pip install -e ../ardupilot-log-analyzer
```

Needs Python 3.11+. Nothing else.

## 2. Check the install

```bash
sentinel doctor
```

Names anything missing and the exact fix for it. If it is happy, continue.

## 3. Run the test suite

```bash
pytest -q
```

Expect:

```
218 passed, 2 skipped
```

Takes about 35 s. Every one of those tests is offline — no simulator, no network, no key. About
30 s of the runtime is a single gate that re-scores every committed result file and checks the
published numbers still regenerate. That gate exists because they once silently stopped
regenerating for a day while the suite stayed green.

## 4. Reproduce the headline result

This is the part worth your attention. It scores three real captured flights and prints the
comparison:

```bash
python scripts/e4_report.py --bundles bundles \
  --verdicts results/crossmodel/gpt2_run1.json --only compass_offset
```

You should see `B0` — the free rule, no AI in it — score **0.00**:

```
judge    acc 95% CI (n=bundles)    bundles  judg   sym  hal  miss  cite  deg
B0      0.00 [0.00-0.56]            0/3        3     3    0     0     0    0
B1      1.00 [0.44-1.00]            3/3        9     0    0     3     0    0
B3      1.00 [0.44-1.00]            3/3        9     0    0     0     1    0
```

**That 0.00 is the whole point.** `B0` is a deterministic rule — *"the first alarm after something
changed is the cause"* — with no model in it. On this fault it is wrong every single time, because
the detector that finds the true cause deliberately waits one second before reporting it, while
the detector that sees the *symptom* fires immediately. So the cheap answer and the correct answer
are different by construction, and anything more expensive has to actually earn its cost.

The `sym` column is B0 naming a **symptom** as the root cause on all 3 flights. That is not a bad
score, it is the specific wrong answer the fault was built to provoke.

> **Do not read B1 and B3 off that single file.** It is one run, and this project's own rule is
> that a single run is not a result — the measured spread is 0.11, one judgement in nine, and
> three of its six retractions came from quoting one run as though it were a mean. The published
> figures are means over five runs (`run1`…`run5` in the same folder): gpt-5.6-sol scores
> **0.71** at B1 and **0.96** at B3. B0 is the exception, and only because it is deterministic
> code with no model in it — it returns exactly 0.00 on every run, forever, which is why it can
> be quoted from one file.

> One honest caveat, stated here rather than left for you to find: every conclusion in this
> project about *model* behaviour rests on 3 flights of this one fault. The fault itself is
> robust. What it implies about AI is not established. See
> [WHITEPAPER.md](WHITEPAPER.md#what-survived-and-what-i-do-not-know).

## 5. Point it at your own flight

```bash
sentinel analyze YOURFLIGHT.BIN
sentinel analyze YOURFLIGHT.BIN --html report.html
```

Reads the ArduPilot dataflash `.BIN` already on your SD card. Every finding prints the measured
value against the threshold actually loaded on the aircraft, so you can check it rather than
trust it. The HTML report is self-contained — no CDN, no fonts, no scripts — so it opens on a
hangar laptop with no network.

For a live link instead of a log:

```bash
sentinel watch --conn COM5,57600           # radio
sentinel watch --conn udp:0.0.0.0:14550    # WiFi or simulator
```

---

## 6. Optional — run the AI judges without paying

Steps 1–5 need no key at all. This step is only if you want to run the judges yourself.

The judge speaks plain **OpenAI-compatible HTTP**, so any endpoint that implements that API works.
Point `--base-url` at it:

```bash
export OPENAI_API_KEY=...
python scripts/e4_judge.py --bundles bundles --only compass_offset \
  --judges B0,B1,B3 --provider openai --model MODEL --base-url ENDPOINT --out verdicts.json

python scripts/e4_report.py --bundles bundles --verdicts verdicts.json --only compass_offset
```

| Route | Endpoint | Notes |
|---|---|---|
| **Ollama, local** | `http://localhost:11434/v1` | Genuinely free and needs no account. Use `OPENAI_API_KEY=none` — any non-empty string works. Best option for just trying it. |
| **OpenRouter** | `https://openrouter.ai/api/v1` | Aggregates many providers behind one key and carries free-tier models (conventionally suffixed `:free`). |
| **NVIDIA NIM** | `https://integrate.api.nvidia.com/v1` | Hosts the Nemotron family with a free developer tier. |
| **xAI** | `https://api.x.ai/v1` | Paid. |
| **llmapi gateway** | `https://api.llmapi.ai/v1` | What the published numbers were measured on. Paid. |

**Free tiers change faster than this file does.** Treat the table as *where to look*, not as a
promise about current quotas or which models are free today — check the provider before assuming.
The mechanism (any OpenAI-compatible base URL) is the stable part.

Two things that will bite you, so they are stated rather than left to discover:

* **B3 needs tool calling.** The agent judge queries the flight through tools, and plenty of
  small or free-tier models either lack tool support or use it unreliably. If B3 fails or returns
  nothing while B1 works, that is a *capability* result, not the model losing the task — the
  harness attributes transport and tool failures to `HARNESS`, never to the model, precisely so
  this does not get miscounted as a wrong answer.
* **B0 must still score 0.00.** It is deterministic and has no model in it, so it does not care
  which endpoint you used. If your run shows B0 as anything else, something is wrong with the
  setup and nothing else in that run is readable.

And if you publish anything from it: **name the model.** Two models in this project ranked the
same judges in opposite orders on identical data, so a result attributed to "an LLM" is not merely
vague — it may be false.

---

## Things that will not work, stated up front

| | |
|---|---|
| **PX4** | Not supported, deliberately. Different dialect, different log format, different parameter names. It would run and find nothing, which is worse than refusing. |
| **Betaflight / INAV** | Not supported. No MAVLink telemetry of this shape. |
| **`pip install sentinel-realtime`** | Not on PyPI yet. Hence the two clones. |
| **The two repos in unrelated folders** | `flightdx` is imported by path in places. Keep them siblings. |
| **Running the AI judges** | Needs *some* endpoint — but not a paid one. See step 6; Ollama runs locally with no account. Steps 1–4 need nothing at all, and they are the parts that prove the claim. |

## Where to go next

| You want | Read |
|---|---|
| What this measures and what survived | [WHITEPAPER.md](WHITEPAPER.md) |
| How to build a test like this in *your* domain | [docs/METHOD.md](docs/METHOD.md) |
| Running it on your own aircraft, properly | [docs/SETUP.md](docs/SETUP.md) |
| What it is and how it is built | [README.md](README.md) |
