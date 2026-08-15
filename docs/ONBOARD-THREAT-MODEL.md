# Onboard assistant over 4G — threat model

**Status: design review, nothing built.** This is the security analysis for a proposed companion
computer that runs on the aircraft, ingests MAVLink from the flight controller, and reaches a
remote API over a 4G modem.

Read the architectural constraint first. Everything after it is detail.

---

## 0. The one constraint that cannot be traded

This repository has a standing rule: **no LLM anywhere near flight control**, enforced by a test
that no model client is imported in `runner.py`, `capture.py` or `gate.py`.

Putting an internet-connected computer on the airframe is the single largest threat to that rule,
because it creates a path that did not previously exist:

```
attacker -> cellular network -> modem -> companion -> MAVLink -> flight controller
```

**The design is only acceptable if that path is severed by construction, not by policy.**
Concretely:

| rule | why |
|---|---|
| The MAVLink connection is **read-only**. No `command_long`, no `param_set`, no mode changes, ever. | A companion that can write to the FC is a remote control surface the moment it is compromised. |
| **No API response may influence flight.** Not commands, not parameters, not advisory text shown to a pilot mid-flight. | An advisory is an input to a human control loop. "Your compass is failing, land now" from a spoofed endpoint is an attack. |
| The aircraft must fly **identically with the link dead**, and that must be the tested default. | If behaviour depends on connectivity, loss of connectivity is a denial-of-service against the aircraft. |
| Telemetry flows **out only**. The uplink carries nothing but transport acknowledgements. | A one-way pipe cannot be turned around. |

If any of those four is relaxed, this stops being a monitoring product and becomes a remotely
commandable aircraft, and the threat model below is no longer sufficient.

The existing architecture already points the right way: detection is local and deterministic, and
the LLM judges run **offline on frozen bundles**. The onboard version should keep exactly that
split — detect on board, ship a `RunBundle` up, never take an answer down.

---

## 1. Attack surfaces

### A. MAVLink side (aircraft-local)

| # | Vector | Notes |
|---|---|---|
| A1 | **MAVLink has no authentication by default.** Anyone within radio range can inject well-formed messages. | The companion currently trusts the FC implicitly. Injected `VIBRATION` or `EKF_STATUS_REPORT` produces fabricated advisories that look identical to real ones. |
| A2 | MAVLink2 signing exists but is **unaffordable on the common link** — +13 B/msg on a stream already at 164% of a 57600 radio (see `MAVLINK-EFFICIENCY.md`). | The secure configuration is the one that does not fit. This is a real protocol-design problem, not an oversight by operators. |
| A3 | Replay of a previously captured stream. | Timestamps in MAVLink are sender-supplied. |
| A4 | Malformed/fuzzed messages against the parser. | `pymavlink` is the parsing surface. Python is memory-safe, so this is a crash/DoS risk rather than RCE — but a crashed monitor is a silent monitor. |

### B. Cellular side

| # | Vector | Notes |
|---|---|---|
| B1 | **The modem is a second computer with its own firmware**, often closed, frequently unpatched. Quectel/SIMCom modules have a CVE history. | It sits between the companion and the network and is not auditable. |
| B2 | **AT command injection** over the modem's serial interface. | If any field reaching the modem is attacker-influenced, this is command execution on the modem. |
| B3 | IMSI catcher / rogue base station. | Downgrade attacks force weaker ciphers; the device may not notice. |
| B4 | SIM theft from a recovered airframe. | A crashed or stolen aircraft yields a SIM with an active data plan and whatever it is authorised to reach. |
| B5 | **TLS verification disabled** — the single most common embedded mistake. | Without certificate *and hostname* validation, B3 becomes full MITM. Pin the CA. |
| B6 | DNS hijack on an untrusted network. | Pin by certificate, not by hostname alone. |

### C. Device and physical

| # | Vector | Notes |
|---|---|---|
| C1 | **API credentials stored on an aircraft that can crash, be stolen, or be bought secondhand.** | Treat every key on the airframe as eventually public. Use short-lived, narrowly-scoped, per-device credentials that can be revoked individually. Never a shared account key. |
| C2 | Debug interfaces left enabled: UART console, SSH with default credentials, ADB. | The overwhelmingly common finding in drone security audits. |
| C3 | Unsigned firmware / writable rootfs. | An attacker with brief physical access owns every future flight. |
| C4 | SD card extraction. | Logs contain flight paths, customer sites, operating patterns. Encrypt at rest. |
| C5 | Supply chain of the Python dependency tree on the airframe. | `pydantic`, `pymavlink` and transitively more. Pin hashes; the current CI has dependabot and CodeQL, which is the right start. |

### D. API and cloud side

| # | Vector | Notes |
|---|---|---|
| D1 | **Response parsing is untrusted input executing on an aircraft.** | Even with a one-way rule, the device parses HTTP responses. Bound sizes, reject unexpected fields, never `eval`. |
| D2 | Compromised endpoint issuing crafted responses to a fleet. | The blast radius is every aircraft at once. This is why D1 and the four rules in §0 matter more than any single control. |
| D3 | Quota exhaustion / billing DoS. | An attacker who can trigger uploads can make flying expensive. Rate-limit on the device, not only server-side. |
| D4 | **Telemetry is surveillance data.** Flight paths reveal customer sites, infrastructure, patterns of life. | In India this also engages DGCA rules and data-localisation expectations. Minimise what leaves the aircraft; a `RunBundle` already excludes wall-clock host detail by design. |

### E. Availability and safety interactions

| # | Vector | Notes |
|---|---|---|
| E1 | 4G dropout mid-flight — routine, not exceptional. | Must be a no-op. If it is not, see §0. |
| E2 | Companion CPU starvation affecting the FC. | Physically separate boards; never run this on the flight controller. |
| E3 | Power draw and brownout of a shared rail. | A companion that browns out the FC is a flight-safety issue, not an IT issue. |
| E4 | Thermal throttling on a sealed airframe in Indian summer conditions. | Measured cycle cost is 11.5% peak of budget on a laptop; a Pi-class board at 5–8× slower is ~69% at peak. Thermal throttling on top of that is the margin gone. |

---

## 2. What the measurements already say

* **Compute is not the problem.** 2,973 measured cycles: p95 14.4 ms, peak 114.6 ms against a
  1,000 ms budget — 11.5% peak. On a Pi-class board (5–8× slower) the peak lands near 690 ms:
  survivable, uncomfortable, and the tail is `build_ms` (bundle serialisation), not detection.
  **Do not build bundles on the flight-critical path.**
* **The link is the problem.** 56.1% of streamed bytes are discarded, and the full stream needs
  164% of a 57600 radio. Fix stream selection before considering a faster radio or a modem.
* **Rust would not help here.** Python is memory-safe; the exposed parsing surface is
  `pymavlink`, and `pydantic-core` is already Rust. The real gaps are the four in §0, TLS
  validation (B5), and credential scoping (C1) — none of which are language choices.

## 3. Recommended shape, if this is built

1. **Two boards.** FC and companion physically separate, separate power rails.
2. **Read-only MAVLink.** Enforced by a test, the way the no-LLM-in-control rule is today.
3. **Store-and-forward, not stream.** Detect locally, write a `RunBundle`, upload when a link
   exists. Nothing waits on the network.
4. **Upload only.** No command channel. No response acted upon beyond an ACK.
5. **Per-device short-lived credentials**, revocable individually, scoped to upload only.
6. **TLS with pinned CA and hostname verification**, failing closed.
7. **Encrypted storage at rest.**
8. **Selective MAVLink stream rates** — 164% → 72% of the radio.
9. **A kill switch that is a physical disconnect**, not a software flag.

## 4. What I would not do

* Put a model — local or remote — anywhere that can influence flight. The existing rule is right.
* Ship advisory text to a pilot in flight from a remote endpoint. That is a control input wearing
  a text label.
* Enable MAVLink2 signing on a 57600 link without first fixing stream selection; it will not fit.
* Rely on the modem being trustworthy. It is not auditable and it is not yours.
