"""Self-contained HTML flight report (Phase E4, product layer).

A terminal transcript is proof for the person who ran it and nothing to anyone else. The people
who act on these findings -- a maintenance lead, a customer, a chief pilot, an auditor -- want a
file they can read, forward and file.

Three rules the format follows, all inherited from the rest of the project rather than invented
for the browser:

  * **Every finding carries its evidence** -- the measured value against the threshold that was
    actually loaded on the aircraft. A report you cannot check is one you have to trust.
  * **Nothing is claimed that was not observed.** Detectors that could not run are listed as
    skipped, not silently omitted, because "no finding" and "never checked" look identical to a
    reader and mean opposite things.
  * **Self-contained.** No CDN, no fonts, no scripts -- it opens from an email attachment on a
    machine with no network, which is the situation of most hangars.

This is also the shape L3 needs: an evidence bundle a human signs. Nothing here is signed yet
and the footer says so.
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

SEVERITY = {
    "critical": ("#7f1d1d", "#fef2f2", "CRITICAL"),
    "warning": ("#92400e", "#fffbeb", "WARNING"),
    "info": ("#1e40af", "#eff6ff", "INFO"),
}

_CSS = """
*{box-sizing:border-box}
body{font:15px/1.55 -apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif;color:#111827;
     margin:0;padding:40px 24px;background:#f3f4f6}
.page{max-width:860px;margin:0 auto;background:#fff;padding:44px 48px;border-radius:10px;
      box-shadow:0 1px 3px rgba(0,0,0,.12)}
h1{font-size:26px;margin:0 0 4px}
.sub{color:#6b7280;font-size:14px;margin-bottom:22px}
.meta{display:flex;flex-wrap:wrap;gap:26px;padding:16px 0;border-top:1px solid #e5e7eb;
      border-bottom:1px solid #e5e7eb;margin-bottom:28px}
.meta div{font-size:13px;color:#6b7280}
.meta b{display:block;font-size:19px;color:#111827;font-weight:700}
.f{border-left:4px solid #d1d5db;padding:16px 20px;margin:0 0 16px;border-radius:0 8px 8px 0}
.f h3{margin:0 0 4px;font-size:17px;font-family:ui-monospace,Menlo,Consolas,monospace}
.tag{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.9px;padding:2px 8px;
     border-radius:4px;color:#fff;vertical-align:2px;margin-left:8px}
.when{color:#6b7280;font-size:13px;margin-bottom:10px}
.row{font-size:14px;margin:5px 0}
.row span{display:inline-block;min-width:118px;color:#6b7280}
.ev{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;background:#f9fafb;
    border:1px solid #e5e7eb;border-radius:5px;padding:7px 10px;margin-top:9px}
.clean{padding:26px;background:#f0fdf4;border:1px solid #86efac;border-radius:8px;color:#166534}
.note{margin-top:30px;padding-top:18px;border-top:1px solid #e5e7eb;color:#6b7280;font-size:13px}
.warnbox{background:#fffbeb;border:1px solid #fcd34d;border-radius:8px;padding:12px 16px;
         font-size:13px;color:#92400e;margin-bottom:22px}
@media print{body{background:#fff;padding:0}.page{box-shadow:none;padding:0}}
"""


def render(source_name: str, params_count: int, message_types: int, incidents: list,
           explain: dict[str, tuple[str, str]], skipped: list[str] | None = None,
           gate_stats: dict | None = None, bundle_id: str | None = None) -> str:
    """Render one flight report. `incidents` are `flightdx.schema.Incident` objects."""
    e = html.escape
    skipped = skipped or []

    by_type: dict[str, list] = {}
    for inc in incidents:
        by_type.setdefault(inc.type, []).append(inc)
    rank = {"critical": 0, "warning": 1, "info": 2}
    ordered = sorted(by_type.items(), key=lambda kv: rank.get(kv[1][0].severity, 3))

    parts = [
        "<style>", _CSS, "</style>",
        "<div class='page'>",
        f"<h1>Flight report</h1>",
        f"<div class='sub'>{e(source_name)}</div>",
    ]

    if skipped:
        parts.append(
            "<div class='warnbox'><b>Not all checks ran.</b> These detectors could not "
            f"complete on this flight: {e(', '.join(skipped))}. Findings they would have "
            "produced are absent from this report — that is not the same as clean.</div>")

    parts.append("<div class='meta'>")
    parts.append(f"<div><b>{len(by_type)}</b>finding types</div>")
    parts.append(f"<div><b>{params_count}</b>parameters read from aircraft</div>")
    parts.append(f"<div><b>{message_types}</b>message types</div>")
    if gate_stats:
        parts.append(f"<div><b>{gate_stats.get('suppression', 0):.1%}</b>noise suppressed</div>")
        parts.append(f"<div><b>{gate_stats.get('advisories', 0)}</b>advisories raised</div>")
    parts.append("</div>")

    if not ordered:
        parts.append("<div class='clean'><b>No findings.</b> Every check that could run came "
                     "back clean. A clean report means nothing crossed a threshold — not that "
                     "the flight was without fault.</div>")
    else:
        for itype, group in ordered:
            worst = min(group, key=lambda i: rank.get(i.severity, 3))
            colour, bg, label = SEVERITY.get(worst.severity, ("#6b7280", "#f9fafb", "?"))
            meaning, action = explain.get(itype, ("", ""))
            parts.append(f"<div class='f' style='border-left-color:{colour};background:{bg}'>")
            parts.append(f"<h3>{e(itype)}<span class='tag' style='background:{colour}'>"
                         f"{label}</span></h3>")
            parts.append(f"<div class='when'>{len(group)} occurrence(s), first at "
                         f"t = {group[0].t_start:.1f} s</div>")
            if meaning:
                parts.append(f"<div class='row'><span>What it means</span>{e(meaning)}</div>")
                parts.append(f"<div class='row'><span>What to check</span>{e(action)}</div>")
            for ev in worst.evidence[:3]:
                unit = f" {ev.unit}" if ev.unit else ""
                parts.append(
                    f"<div class='ev'>{e(ev.metric)} = <b>{ev.value:g}{e(unit)}</b> "
                    f"&nbsp;·&nbsp; threshold {ev.threshold:g}{e(unit)}</div>")
            parts.append("</div>")

        parts.append(
            "<div class='note'><b>The first alarm is not always the cause.</b> One fault trips "
            "several detectors, and the fastest detector is not the root cause. Findings above "
            "are ordered by severity, not by causality.</div>")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    parts.append(f"<div class='note'>Generated {stamp} by SentinelAgent. Thresholds were read "
                 "from the aircraft's own parameters, not from defaults.")
    if bundle_id:
        parts.append(f"<br>Bundle <code>{e(bundle_id)}</code> — a content hash; anyone with the "
                     "same log reproduces the same identifier.")
    parts.append("<br><b>This report is unsigned and is not a certification artifact.</b> It "
                 "records what automated checks observed, for a qualified person to act on."
                 "</div></div>")
    return "".join(parts)


def write(path: str | Path, **kwargs) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render(**kwargs), encoding="utf-8")
    return p
