# SentinelAgent — root-cause discrimination for UAV telemetry

Can an LLM agent tell the **cause** of a flight fault from its **symptoms** — and does it beat a
free deterministic baseline **at equal cost**?

This repository is the harness that answers that question honestly, including the case where the
answer is "no".

![architecture](docs/e4-architecture.svg)

---

## The problem

One fault trips several detectors. A stiff airframe clips the accelerometer *before* the
vibration detector has enough window to call it. A magnetometer offset raises EKF variance
immediately, while the compass detector waits a full second to confirm the anomaly is sustained.

In both cases the **first** alarm is a symptom, and the deterministic tier answers it with total
confidence. Telling the operator "accelerometer clipping" when the fix is a loose motor mount is
the difference between a wasted maintenance day and a corrected aircraft.

## How it works

**Capture and judgment are separated.** Flying an ArduPilot SITL scenario takes ~4 minutes;
comparing four judges across three prompt paraphrases takes ~96 judgments. So a flight is
captured once into a content-hashed `RunBundle`, and every judge runs offline against the file.

| Stage | What |
|---|---|
| **Capture** | Live SITL → 7 detectors at 1 Hz over a rolling window → escalation gate → advisories. Fault injected with **parameter readback**, so the test condition is proven, not assumed |
| **RunBundle** | Every incident, advisory, parameter and timing from one flight, sha256-identified. Hand-edit it and loading fails |
| **Judge** | Four judges, one interface, all offline: **B0** deterministic · **B1** single LLM call · **B2** N-sample at B3's measured spend · **B3** tool-using agent |
| **Score** | Root-cause-only. Naming a symptom scores 0 |
| **Stats** | Wilson intervals, Cohen's κ, bootstrap CI, prompt flip rate, rule-based failure attribution |

### The rules that make the number mean something

- **Naming a symptom scores zero**, not partial credit. That confusion is the thing being measured.
- **A correct answer with an unresolvable citation scores zero.** Being right while pointing at
  something that never happened is not being right.
- **B2 is held to B3's measured token spend.** A comparison that doesn't control for spend
  measures spend. The *achieved* match is published, not the intended one.
- **A degraded run stays labelled B3.** Relabelling it would delete the agent's failures from the
  table.
- **The ground-truth label is unreachable.** The tool surface is an allow-list and no prompt names
  a fault type; tests assert it against the full transcript the model saw.
- **If the free baseline wins, that gets published.**

## Status — honest

**Measured on ArduPilot SITL:** 4/4 scenarios pass · 0 false positives · 1.0 s detection latency
(vibration), 5.0 s (GPS loss) · 96.9% advisory suppression · worst cycle **18 ms of 1000 ms**.

**Not yet measured:** every judge comparison. B1/B2/B3 have not been run against a live model —
the loop is proven end-to-end against a scripted client, at zero tokens. **No accuracy, κ, or
cost number for any LLM judge exists yet, and none is claimed.**

**Known and recorded:** on the original four scenarios the deterministic baseline **B0 scores
4/4 at zero tokens**. No agent can beat that — the best available outcome is a tie at higher
cost. Two ambiguous faults (`compass_offset`, `stiff_airframe`) were added specifically to give
the comparison headroom, and `stats.ambiguity_worked()` fails the report if a future fault set
loses it again.

```
74 tests, all offline — no simulator, no network, no API key
```

## Running it

```bash
pip install -e .                    # plus flightdx, see below
pytest                              # 74 tests, offline
```

Live capture needs ArduPilot SITL (WSL2) and `pymavlink`:

```bash
python scripts/r7_r8_scenarios.py --bundles bundles --repeat 3 --compare r8_results.json
python run_live.py --conn tcp:127.0.0.1:5760 --duration 30
```

`--conn` takes any pymavlink connection string, so `COM5,57600` (SiK radio) or
`udp:0.0.0.0:14550` work against a real aircraft. Note that a SiK link cannot carry 13 message
types at 10 Hz; below `MIN_ATT_RATE_HZ = 7.0` the oscillation detector **declines to run** rather
than emit an aliased result.

### Dependency

The detector tier lives in [`flightdx`](https://github.com/RahulRajelli/ardupilot-log-analyzer)
(ArduPilot log analysis: parsers, 7 detectors, evidence schema). This repository is the realtime
runner, the escalation gate and the agent evaluation harness on top of it.

## Design rules that do not bend

- **No LLM anywhere near flight control.** Judges are offline and read-only *structurally* — the
  bundle is a frozen file, so there is no write path to expose.
- **No RAG, no vector database.** There is no corpus here; adding one would be architecture
  theater.
- **The uplink is read-and-configure only** — stream rates and parameter fetch. Nothing commands
  the aircraft.

## Licence

Apache-2.0.
