# Security policy

## Scope

This project analyses flight telemetry and evaluates LLM judges. Two properties are
security-relevant and are enforced in code rather than promised:

- **Nothing here can command an aircraft.** The judge tool surface
  (`sentinel/judges/tools.py`) is read-only by construction: it operates on a frozen,
  content-hashed `RunBundle` file, so there is no write path to expose. The live tier's only
  uplink is stream-rate configuration and a parameter fetch.
- **No LLM sits in a control loop.** Judges run offline against captured files.

If you are integrating this into something that flies, those are the two invariants to preserve.

## Reporting a vulnerability

Please open a [private security advisory](https://github.com/RahulRajelli/sentinel-realtime/security/advisories/new)
rather than a public issue. I aim to acknowledge within 7 days.

Include what you did, what happened, and what you expected. A failing test is the most useful
possible report.

## Known limitations

- Detector thresholds are read from the vehicle. A vehicle reporting false parameters produces
  wrong verdicts, and the runner warns when the parameter fetch is incomplete rather than
  silently proceeding.
- Operator free text (device names, mission names, pilot notes) reaches the prompt on the live
  path and is an injection surface. Telemetry itself is machine-generated and is not.
