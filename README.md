# SentinelAgent — root-cause discrimination for UAV telemetry

Can an LLM agent tell the **cause** of a flight fault from its **symptoms** — and does it beat a
free deterministic baseline **at equal cost**?

This repository is two things: a fault-detection tool you can point at a flight log today, and
the harness that answers that question honestly — including when the answer is "no".

![architecture](docs/e4-architecture.svg)

---

## Start here

```bash
pip install -e .          # plus flightdx, see Dependency below
sentinel doctor           # tells you what's installed and what's missing
sentinel analyze YOURFLIGHT.BIN
```

No simulator, no MAVLink link, no API key. It reads the `.BIN` files already on your SD card.
Real output, from a real 3.4 MB log:

```
FLIGHT REPORT  |  2024-04-30 17-30-57.bin
  parameters loaded : 1088
  message types     : 58

  2 finding(s), most serious first:

  !  battery_threshold_misconfigured   [warning]   1 occurrence(s), first at t=463.6s
       what it means : The configured battery failsafe thresholds look inconsistent with the pack.
       what to check : Review BATT_LOW_VOLT / BATT_CRT_VOLT against the pack's real chemistry.
       evidence      : BATT_LOW_VOLT = 10.5 V (threshold 22.2 V)

  !  compass_inconsistency   [warning]   3 occurrence(s), first at t=488.6s
       evidence      : MagFieldDeviation = 0.397 fraction (threshold 0.35 fraction)
```

That first finding is a 3S low-voltage failsafe left configured on a 6S pack — the failsafe
would never have fired. **Every finding prints its evidence**: the measured value against the
threshold that was actually loaded on the aircraft, so you can check it rather than trust it.

| Command | What it does |
|---|---|
| `sentinel doctor` | Checks your machine, names the exact fix for anything missing |
| `sentinel analyze FLIGHT.BIN` | Analyses a log you already have |
| `sentinel replay FLIGHT.BIN` | Replays a real flight **through the realtime tier** (below) |
| `sentinel watch --conn COM5,57600` | Live over a radio, WiFi (`udp:0.0.0.0:14550`) or SITL |
| `sentinel capture / judge / report` | The research path (below) |

Add `--html report.html` to `analyze` or `replay` for a self-contained report you can email —
no CDN, no fonts, no scripts, so it opens on a hangar machine with no network.

### `replay` — because injected faults are not real faults

SITL injects a fault by setting a `SIM_*` parameter: a clean synthetic step at a known instant.
Real faults ramp, intermit, and arrive tangled with wind and throttle changes. A harness measured
only on injected faults measures how well it detects injections.

`replay` drives the **same** rolling buffer, the **same** seven detectors and the **same**
escalation gate from a real `.BIN`. It needs no simulator, no WSL and no ArduPilot toolchain, so
a stranger with the same log can reproduce your result exactly — `bundle_id` is a content hash
over what the flight contained, deliberately excluding wall-clock timings so it fingerprints the
flight rather than the machine.

```
140 cycles · 346 raw detections · 11 advisories · 96.8% suppressed by the gate
```

**That 96.8% is worth pausing on.** The escalation gate measured **96.9% on injected SITL faults**
and **96.8% on a real flight** — produced independently, on completely different data. It is the
first evidence the gate behaves the same on real telemetry as on synthetic.

A replay deliberately does **not** set a ground-truth label. It records what was seen; a human
labels what it means. Deriving the label from the detector output would grade the system against
its own opinion.

`watch --passive` listens at whatever rate your ground station already set and skips the
parameter fetch — for shared radio links where raising stream rates would congest the pilot's
own telemetry. It says so, rather than quietly degrading: without the aircraft's parameters,
actuator and battery verdicts are unreliable.

---

## The problem the agent layer exists for

One fault trips several detectors. A stiff airframe clips the accelerometer *before* the
vibration detector has enough window to call it. A magnetometer offset raises EKF variance
immediately, while the compass detector waits a full second to confirm the anomaly is sustained.

In both cases the **first** alarm is a symptom, and the deterministic tier answers it with total
confidence. Telling an operator "accelerometer clipping" when the fix is a loose motor mount is
the difference between a wasted maintenance day and a corrected aircraft.

## How the measurement works

**Capture and judgment are separated.** Flying an ArduPilot SITL scenario takes ~4 minutes;
comparing four judges across three prompt paraphrases takes ~96 judgments. So a flight is
captured once into a content-hashed `RunBundle`, and every judge runs offline against the file.

| Stage | What |
|---|---|
| **Capture** | Live SITL → 7 detectors at 1 Hz → escalation gate → advisories. Fault injected with **parameter readback**, so the test condition is proven, not assumed |
| **RunBundle** | Every incident, advisory, parameter and timing from one flight, sha256-identified. Hand-edit it and loading fails |
| **Judge** | **B0** deterministic · **B1** single LLM call · **B2** N-sample at B3's measured spend · **B3** tool-using agent |
| **Score** | Root-cause-only. Naming a symptom scores 0 |
| **Stats** | Wilson intervals, Cohen's κ, bootstrap CI, prompt flip rate, rule-based failure attribution |

### The rules that make the number mean something

- **Naming a symptom scores zero**, not partial credit. That confusion is the thing being measured.
- **A correct answer with an unresolvable citation scores zero.** Being right while pointing at
  something that never happened is not being right.
- **B2 is held to B3's measured token spend.** A comparison that doesn't control for spend
  measures spend. The *achieved* match is published, not the intended one.
- **A degraded run stays labelled B3.** Relabelling it would delete the agent's failures.
- **The ground-truth label is unreachable.** The tool surface is an allow-list and no prompt
  names a fault type; tests assert it against the full transcript the model saw.
- **If the free baseline wins, that gets published.**

---

## Model choice and running cost

**The escalation gate is what makes this cheap.** It absorbs **96.9%** of raw detector output, so
the LLM is invoked per *escalation*, not per cycle — on the order of tens of calls a day for a
100-aircraft fleet, not tens of thousands.

**Every judge talks to a one-method interface** (`ModelClient.complete()` in
`sentinel/judges/model.py`). Swapping the model — or the provider entirely — is implementing that
one method. `ScriptedClient` and `DryRunClient` already do, which is how the whole test suite
runs at zero cost.

**Estimated cost — not yet measured.** No LLM judge has been run against a live model, so these
are arithmetic from published per-token prices and expected prompt sizes, not observations. The
harness reports real `tok/judgement` once a sweep runs; trust that column, not this table.

| | Opus-tier ($5/$25 per MTok) | Haiku-tier ($1/$5 per MTok) |
|---|---|---|
| One agent judgement (~5 calls) | ~$0.12 | ~$0.025 |
| Full sweep (12 bundles × 3 variants × 4 judges + grader) | **~$10** | ~$2 |
| Fleet operation (~40 escalations/day) | ~$5/day | ~$1/day |

**So cost is not the binding constraint here — capability is.** A full experimental sweep costs
about what a coffee does. Picking a cheaper model *before* measuring which model can do the task
at all would be optimising the wrong variable. Run the sweep on the capable model first; the
table will tell you how much headroom a cheaper one has.

**On free and local models.** The interface is provider-agnostic, so a local server (llama.cpp,
Ollama, vLLM) is a legitimate `ModelClient`. Two honest caveats: small local models are
substantially weaker at *reliable tool use*, which is exactly what B3 needs, and an 8 GB consumer
GPU limits you to ~8B quantised. The realistic use is a local model as an **extra B1-style row**
in the table — a cheap single-shot baseline — not as the agent. And that is a measurement, not a
guess: add the row and the scorer will tell you.

**On fine-tuning.** Not yet viable, and worth saying plainly: fine-tuning needs training data,
and this repo has a handful of scenarios. Anything trained on that overfits. Revisit when the
task set is 60–150 items — at which point the more valuable artifact is the *labelled set*
itself, not the tuned model.

---

## Status — honest

**Measured on ArduPilot SITL:** 4/4 scenarios pass · 0 false positives · 1.0 s detection latency
(vibration), 5.0 s (GPS loss) · 96.9% advisory suppression · worst cycle **18 ms of 1000 ms**.

**Not yet measured:** every judge comparison. B1/B2/B3 have not been run against a live model —
the loops are proven end-to-end against a scripted client, at zero tokens. **No accuracy, κ, or
cost number for any LLM judge exists yet, and none is claimed.**

**Known and recorded:** on the original four scenarios the deterministic baseline **B0 scores
4/4 at zero tokens**. No agent can beat that — the best available outcome is a tie at higher
cost. Two ambiguous faults (`compass_offset`, `stiff_airframe`) were added specifically to give
the comparison headroom, and `stats.ambiguity_worked()` fails the report if a future fault set
loses it again.

**Two things the API does not provide, recorded because they change what the numbers mean:**
sampling parameters are rejected on the current model, so **B2's k samples vary only by the
model's own non-determinism** — it publishes its observed disagreement rate, and a rate of 0
means B2 collapsed into B1 at k× the cost. And there is **no seed**: bundles are deterministic,
verdicts are not, so this is not end-to-end reproducible and does not claim to be.

```
74 tests, all offline — no simulator, no network, no API key
```

## Running the research path

```bash
sentinel capture --bundles bundles --repeat 3 --compare r8_results.json   # needs WSL + SITL
sentinel judge   --bundles bundles --dry-run                              # free, exercises everything
sentinel judge   --bundles bundles                                        # needs an API key
sentinel report  --bundles bundles --verdicts verdicts.json --markdown
```

`--dry-run` runs the entire sweep against a stub that answers "the first advisory" — the naive
strategy — so you see the real shape of the table before spending a token.

### Dependency

The detector tier lives in [`flightdx`](https://github.com/RahulRajelli/ardupilot-log-analyzer)
(ArduPilot log parsing, 7 detectors, evidence schema). This repository is the realtime runner,
the escalation gate, the CLI and the agent evaluation harness on top of it.

A SiK radio cannot carry 13 message types at 10 Hz; below `MIN_ATT_RATE_HZ = 7.0` the oscillation
detector **declines to run** rather than emit an aliased result. That is why the on-vehicle
companion-computer topology exists — detect at full rate, send events down the link, not telemetry.

## Design rules that do not bend

- **No LLM anywhere near flight control.** Judges are offline and read-only *structurally* — the
  bundle is a frozen file, so there is no write path to expose.
- **No RAG, no vector database.** There is no corpus here; adding one would be architecture theater.
- **The uplink is read-and-configure only** — stream rates and parameter fetch. Nothing commands
  the aircraft.

## Licence

Apache-2.0.
