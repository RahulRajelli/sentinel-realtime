# Setup — running this on your own aircraft

Two ways in, and the first needs no hardware, no simulator and no AI account:

| you have | start with |
|---|---|
| a `.BIN` log on an SD card | [Analyse a log](#1-analyse-a-log-you-already-have) |
| a live radio or WiFi link | [Watch a live flight](#3-watch-a-live-flight) |
| neither, just curious | [Run the simulator](#4-run-the-simulator) |

---

## Supported platforms — read this first

**ArduPilot only.** This is not a limitation that is about to be lifted quietly, so it is stated
before the install instructions rather than after:

* Live telemetry is parsed with the `ardupilotmega` MAVLink dialect (`sentinel/cli.py`).
* Log analysis reads ArduPilot **dataflash** `.BIN` files.
* The detectors read ArduPilot message names (`VIBE`, `XKF4`, `RCOU`, `ATT`, `MAG`, `GPS`, `BAT`)
  and ArduPilot parameter names (`BATT_LOW_VOLT`, `INS_*`, `COMPASS_*`).

| stack | status |
|---|---|
| ArduPilot — Copter, Plane, Rover | **supported**, Copter is what is tested |
| PX4 | **not supported.** Different dialect, different log format (ULog), different parameter names. It would run and find nothing, which is worse than refusing |
| Betaflight / INAV | not supported, no MAVLink telemetry of this shape |

Tested on Windows 11 with Python 3.12 and on Ubuntu 24.04 under WSL. Any OS with Python 3.11+
should work for log analysis; live capture additionally needs a serial or UDP link.

---

## Install

```bash
git clone https://github.com/RahulRajelli/sentinel-realtime
cd sentinel-realtime

python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -e .            # log analysis only
pip install -e ".[live]"    # adds live MAVLink capture
```

**The detectors live in a sibling package.** `flightdx` ships in
[ardupilot-log-analyzer](https://github.com/RahulRajelli/ardupilot-log-analyzer) and is not on
PyPI. Clone it next to this repo:

```bash
cd ..
git clone https://github.com/RahulRajelli/ardupilot-log-analyzer
pip install -e ardupilot-log-analyzer
```

Then confirm the whole thing:

```bash
sentinel doctor
```

`doctor` names the exact fix for anything missing rather than just reporting a failure. If it is
happy, you are set up.

---

## 1. Analyse a log you already have

```bash
sentinel analyze FLIGHT.BIN
sentinel analyze FLIGHT.BIN --html report.html
```

The HTML report is self-contained — no CDN, no fonts, no scripts — so it opens on a hangar
machine with no network, and it can be emailed as one file.

### Where your `.BIN` actually is

| source | path |
|---|---|
| SD card, direct | `APM/LOGS/` on the card |
| Mission Planner | *Data* → *DataFlash Logs* → *Download DataFlash Log Via Mavlink* |
| QGroundControl | *Analyze* → *Log Download* |
| over MAVLink | `mavproxy.py --master=COM5 --baudrate 57600` then `module load ftp` |

Logs are typically 1–20 MB. Analysis is local; nothing is uploaded anywhere.

---

## 2. Replay a real flight through the realtime tier

```bash
sentinel replay FLIGHT.BIN
```

`analyze` runs the detectors over the whole file at once. `replay` feeds the same log through the
**live** path — rolling 120-second buffer, escalation gate, advisories in time order — so you see
what the monitor would have told the operator during the flight rather than what a reviewer can
see afterwards.

Use this before trusting `watch` on a real aircraft. It is the same code path with none of the risk.

---

## 3. Watch a live flight

```bash
# Windows, telemetry radio on COM5
sentinel watch --conn COM5,57600 --passive

# Linux, USB radio
sentinel watch --conn /dev/ttyUSB0,57600 --passive

# UDP, e.g. forwarded from a ground station
sentinel watch --conn udp:0.0.0.0:14550 --passive
```

**Always start with `--passive`.** Without it the tool requests message streams from the
autopilot; passive mode only listens to what is already being sent. On a first flight with a new
setup, listen before you ask.

### A serial port holds one program at a time

If Mission Planner or QGC already has the radio open, `sentinel watch` cannot also have it. Pick one:

* **Forward from your GCS.** Mission Planner: *Ctrl-F* → *Mavlink* → forward to
  `udp:127.0.0.1:14550`, then `--conn udp:0.0.0.0:14550`.
* **Use MAVProxy as a mux.**
  `mavproxy.py --master=COM5,57600 --out=udp:127.0.0.1:14550 --out=udp:127.0.0.1:14551`
* **Use a second radio** on a different port.

### Coverage — check before you trust silence

One detector needs attitude telemetry at **7 Hz or better**, and turns itself off below that.
Since 2026-08-15 the bundle records which detectors could actually evaluate, so a quiet screen can
be told apart from a detector that never ran. If you are on a slow link, expect
`control_oscillation` to be blind and treat its silence as no information rather than as good news.

---

## 4. Run the simulator

No aircraft needed. SITL is ArduPilot's software-in-the-loop simulator.

```bash
# Ubuntu / WSL, once
git clone https://github.com/ArduPilot/ardupilot && cd ardupilot
git submodule update --init --recursive
./waf configure --board sitl && ./waf copter

# then, from this repo
python scripts/r7_r8_scenarios.py --scenarios null,vibration,gps_loss,wind \
  --bundles bundles --repeat 1
```

That flies four scenarios, injects a fault into three of them, and prints a table of what was
detected, how fast, and how much was suppressed. It is the fastest way to see the whole system
work end to end.

---

## 5. The research path — measuring an LLM judge

Only needed if you care about the evaluation harness rather than the monitor.

```bash
pip install -e ".[openai]"     # or ".[gemini]"

export OPENAI_API_KEY=...
export OPENAI_BASE_URL=https://api.llmapi.ai/v1   # any OpenAI-compatible gateway

python scripts/e4_judge.py --bundles bundles --only compass_offset \
  --judges B0,B1,B3 --provider openai --model gpt-5.6-sol --out verdicts.json

python scripts/e4_report.py --bundles bundles --verdicts verdicts.json --only compass_offset
```

`--provider gemini` uses Google application-default credentials instead. Any OpenAI-compatible
endpoint works: omit `--base-url` for OpenAI, `https://api.x.ai/v1` for Grok,
`http://localhost:11434/v1` for a local server.

**Run five times and report a mean.** These models are not deterministic even at temperature 0;
measured spread is one judgement in nine, and single runs have produced two retracted claims here.

---

## Troubleshooting

| symptom | cause |
|---|---|
| `sentinel: command not found` | virtualenv not activated, or `pip install -e .` not run |
| `ModuleNotFoundError: flightdx` | the sibling package is not installed — see Install |
| `no heartbeat` on `watch` | wrong baud, wrong port, or a GCS already holds the port |
| Analysis finds nothing on a PX4 log | PX4 is not supported. See Supported platforms |
| `control_oscillation` never appears | usually correct — it needs ATT at 7 Hz+ and a commanded attitude. Check `detector_coverage` |
| Windows console shows `?` | the console is not UTF-8; output is deliberately ASCII, so this is cosmetic |

## What this does not do

Stated plainly, because the gap between what a monitor detects and what an operator assumes it
detects is where people get hurt:

* **It does not control the aircraft.** No model client is imported anywhere in the live path, and
  that is a design rule rather than an accident.
* **It names one of six fault types.** A failure outside that vocabulary produces silence.
* **The detectors are validated against injected simulator faults**, not against a labelled corpus
  of real failures. Thresholds are defaults and may not suit your airframe.
* **It is a second opinion, not a safety system.** Nothing here is certified for anything.
