# MAVLink link efficiency — measured, and what can be done about it

**Measured 2026-08-15** on ArduPilot SITL (arducopter, quad), armed and airborne, after
`request_data_stream_send(MAV_DATA_STREAM_ALL, 10 Hz)` — the request this project's own live tier
makes today. 20.1 s sample, every message counted with its real on-wire length
(`len(msg.get_msgbuf())`), not an estimate from the schema.

## The numbers

| | B/s | share |
|---|---|---|
| total streamed | **9,442** | 100% |
| consumed by the detectors | 4,146 | 43.9% |
| **discarded on arrival** | **5,296** | **56.1%** |

Against real telemetry hardware:

| link | capacity | full stream | only what is used |
|---|---|---|---|
| 57600 baud (SiK radio, the common case) | ~5,760 B/s | **164% — oversubscribed** | 72% |
| 115200 baud | ~11,520 B/s | 82% | 36% |

**A 57600 telemetry radio cannot carry this stream.** It is over capacity by two thirds before
anything useful is done with it. What actually happens on such a link is queue growth, latency
climbing without bound, and eventually dropped messages — which this project's `health.py` would
report as "the monitor fell behind", correctly, while the real cause sits one layer down.

## Where the waste is

Highest-cost streams that are never read by any detector:

| message | B/s | why it is not needed |
|---|---|---|
| `SIMSTATE` | 560 | simulator ground truth; does not exist on real hardware |
| `ESC_TELEMETRY_1_TO_4` | 560 | no ESC detector exists (vocabulary ceiling) |
| `RC_CHANNELS` | 540 | pilot stick positions; no detector consumes them |
| `RAW_IMU` | 410 | `SCALED_IMU2` is the one the vibration path uses |
| `LOCAL_POSITION_NED` | 400 | `GLOBAL_POSITION_INT` already carries what is used |
| `AHRS` / `AHRS2` | 758 | `EKF_STATUS_REPORT` is the filter-health source |
| `SCALED_IMU3` | 330 | third IMU, unused |
| `SCALED_PRESSURE` / `2` | 520 | no barometer detector |
| `TERRAIN_REPORT` | 310 | unused |
| `SYSTEM_TIME`, `MEMINFO` | 412 | unused |

## The cheap fix, and its exact size

`MAV_DATA_STREAM_ALL` is a blunt instrument: it is a request for *everything at one rate*. The
`SRx_*` parameters (`SR1_EXTRA1`, `SR1_EXTRA2`, `SR1_POSITION`, `SR1_RAW_SENS`, …) set per-group
rates instead, and MAVLink2 additionally offers `MAV_CMD_SET_MESSAGE_INTERVAL` for per-message
rates.

Requesting only the groups carrying the 13 consumed types takes the link from **164% → 72%** of a
57600 radio. That is the difference between a saturated link and a working one, and it is a
configuration change rather than a protocol change.

**This has a cost worth stating.** `--passive` exists precisely because raising stream rates on a
shared radio congests the pilot's own telemetry. Selective rates make that better, not worse — but
any tool that reconfigures rates on a link it does not own is taking bandwidth from whoever else
is using it. Setting rates should stay opt-in.

## Protocol-level inefficiencies, and honest limits on fixing them

These are properties of MAVLink itself. Listed because they bound what tuning can achieve, not as
a proposal to replace the protocol — a bespoke protocol would lose every ground station, log
analyser and autopilot that speaks this one, which is a far worse trade than 56% of a radio link.

1. **Fixed-rate push, not demand-driven.** Rates are set once; a consumer that needs `ATTITUDE` at
   10 Hz only while diagnosing pays for it continuously. `SET_MESSAGE_INTERVAL` helps and is
   under-used.
2. **No delta encoding.** `SYSTEM_TIME` and `MEMINFO` are near-constant between frames and are
   retransmitted in full at every tick.
3. **No compression.** Telemetry is highly compressible; nothing in the framing allows it.
4. **Per-message overhead.** MAVLink2 costs 12 bytes of framing per message. At 10 Hz across ~24
   message types that is ~2.9 kB/s of pure framing — **31% of the measured total**, before any
   payload. Small, frequent messages are the expensive pattern.
5. **Signing costs more.** MAVLink2 signing adds 13 bytes per message, another ~3.1 kB/s at this
   rate. On a link already at 164%, the security feature is unaffordable — which is a large part
   of why nobody enables it. That is a genuine protocol-design problem: **the secure configuration
   is the one that does not fit.**

## What this project should actually do

1. **Request only what is consumed.** 164% → 72% on the common radio. Biggest win, no downside
   beyond the shared-link caveat above.
2. **Say so when the link is the constraint.** `health.py` reports "the monitor fell behind"
   without distinguishing a slow monitor from a saturated radio. Those need different fixes and
   currently look identical to an operator.
3. **Do not invent a protocol.** Interoperability is worth more than the bytes.

*Regenerate: `_mavlink_bytes.py` pattern in this repo's history, or point any MAVLink client at a
SITL instance and sum `len(msg.get_msgbuf())` by type.*
