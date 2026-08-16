# Quickstart — five minutes, no hardware

**You do not need a drone, a simulator, an AI account, or an API key to check that this works.**
The flights are already captured and committed, so the whole thing runs offline on your machine.

If any step does not print roughly what is shown below, that is a bug and worth an issue.

---

## 1. Install

```bash
pip install sentinel-realtime
```

That pulls `flightdx` — the parsers and the seven detectors — with it as a declared dependency.
Python 3.11+. Nothing else: no drone, no simulator, no API key, no account.

Steps 3 and 4 below reproduce the published figures, and those need the committed flights, which
ship in the repository rather than in the wheel:

```bash
git clone https://github.com/RahulRajelli/sentinel-realtime
cd sentinel-realtime
pip install -e ".[dev]"
```

Working on the detectors themselves? Clone `ardupilot-log-analyzer` next to this one and
`pip install -e` that too; `conftest.py` puts the sibling `src/` tree on the path.

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
| **Unsloth Desktop, local** | `http://localhost:8000/v1` (or your `-p` port) | Free, open source, runs and fine-tunes locally. Needs a real key from the app. See below. |
| **OpenRouter** | `https://openrouter.ai/api/v1` | Aggregates many providers behind one key and carries free-tier models (conventionally suffixed `:free`). |
| **NVIDIA NIM** | `https://integrate.api.nvidia.com/v1` | Hosts the Nemotron family with a free developer tier. |
| **xAI** | `https://api.x.ai/v1` | Paid. |
| **llmapi gateway** | `https://api.llmapi.ai/v1` | What the published numbers were measured on. Paid. |

**Free tiers change faster than this file does.** Treat the table as *where to look*, not as a
promise about current quotas or which models are free today — check the provider before assuming.
The mechanism (any OpenAI-compatible base URL) is the stable part.

### Running it against a local model with Unsloth Desktop

> **Written from Unsloth's documentation, not from a run of this harness against it.** Nothing in
> this project has been measured on Unsloth. Treat the commands as a starting point and the
> numbers you get as yours, not as a result of this repository. Unsloth Desktop shipped
> 2026-08-11, so it is new enough that its flags may have moved by the time you read this.

Install, then serve a model as an OpenAI-compatible endpoint:

```bash
curl -fsSL https://unsloth.ai/install.sh | sh     # macOS / Linux / WSL
# Windows PowerShell:  irm https://unsloth.ai/install.ps1 | iex

unsloth run --model unsloth/Muse-Glimmer-30B-GGUF:UD-Q4_K_XL -p 8000 --disable-tools
```

Unsloth's catalog carries the current agentic models as Dynamic GGUF quants -- Muse Glimmer 30B,
Qwen3.8 27B, Gemma 4. **Read the memory caveat below before picking one**: the interesting ones
are 27-31B and do not fit a laptop GPU.

The API key is generated inside the app — **Settings → API → Create** — and starts `sk-unsloth-`.
Unlike Ollama, a placeholder will not do:

```bash
export OPENAI_API_KEY=sk-unsloth-...
python scripts/e4_judge.py --bundles bundles --only compass_offset \
  --judges B0,B1,B3 --provider openai --model <the-model-you-served> \
  --base-url http://localhost:8000/v1 --out verdicts-local.json
```

Three things worth knowing before you spend an evening on it:

* **`--disable-tools` is deliberate.** Unsloth enables its own web search, code execution and
  bash for clients on localhost. This harness supplies its own read-only tools and needs none of
  that, so leaving them on adds a local code-execution surface for no benefit. Turning them off
  does not affect the function-calling B3 relies on, which is the OpenAI tools parameter and a
  different mechanism.
* **Check the model fits before blaming the result.** A model that is swapping or truncating
  produces a *hardware* result wearing a capability costume, and the harness cannot tell the
  difference. Unsloth publishes the memory each quant needs — for Muse Glimmer 30B it is 12–14 GB
  at 2-bit, 17 GB at 4-bit. The current agentic models in that family (Muse Glimmer 30B,
  Qwen3.8 27B, Gemma 4 31B) therefore do **not** fit an 8 GB consumer card at any usable quant.
  llama.cpp will still run them by offloading to system RAM or disk, and it will be slow.

  Slow matters more here than it looks, because the protocol is not one prompt. A published B3
  arm is 3 bundles x 3 variants x 5 runs = 45 judgements at roughly 2,150 tokens each, and B3
  averages two model calls per judgement. At offloaded speeds that is hours per arm, not minutes.
  If your card is 8 GB, either size down to something that sits entirely in VRAM (~8B class at
  4-bit) and accept it as an extra B1-style row, or run one bundle and one variant as a probe and
  say so when you report it. Do not start a five-run sweep you will not finish.
* **The cost table does not transfer.** `scripts/e4_cost.py` reports tokens per correct answer,
  and local tokens are free at the margin, so a local model scores unbeatably well on a metric
  that has stopped measuring anything. If you want a comparable number for local inference, the
  honest one is wall-clock or energy per correct answer, and this repo does not measure either.

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
| **The two repos in unrelated folders** | Only matters for an editable dev install — `flightdx` is imported by path there. A plain `pip install` needs neither clone. |
| **Running the AI judges** | Needs *some* endpoint — but not a paid one. See step 6; Ollama runs locally with no account. Steps 1–4 need nothing at all, and they are the parts that prove the claim. |

## Where to go next

| You want | Read |
|---|---|
| What this measures and what survived | [WHITEPAPER.md](WHITEPAPER.md) |
| How to build a test like this in *your* domain | [docs/METHOD.md](docs/METHOD.md) |
| Running it on your own aircraft, properly | [docs/SETUP.md](docs/SETUP.md) |
| What it is and how it is built | [README.md](README.md) |
