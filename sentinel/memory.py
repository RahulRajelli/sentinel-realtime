"""Cross-flight history for one airframe (Phase E5).

**What this fixes.** Until now the system's entire temporal context was `RollingBuffer`'s 120
seconds. Everything older was gone, and every flight was judged as though it were the first one
that aircraft had ever flown. A maintainer does not work that way: "this airframe has thrown a
compass anomaly on three of its last eight flights" is often the whole diagnosis, and no amount
of within-flight evidence can produce it.

**The shape.** Append-only JSONL, one line per flight, keyed by airframe. Append-only is the
point: a history that can be rewritten cannot be used as evidence, and a verdict citing it would
inherit that weakness. Reads are filtered by a time window and aggregated; nothing here returns
a raw dump.

**What is deliberately NOT stored.** No ground-truth labels, no expected root cause. The history
is offered to judges as a tool, and a judge that could read `expected_root_cause` from last
week's flight would score well while measuring nothing — the same leak `tools.py` guards against
within a flight, applied across flights. `record_flight` takes a bundle and copies an allow-list.

**Why counts and dates rather than the flights themselves.** Handing a judge four previous
bundles multiplies its input by four and re-creates the payload problem measured on 2026-08-14,
where one unbounded tool result exhausted the token ceiling. A recurrence is a small fact:
how many times, how recently, how many flights ago. That fits in a sentence.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from sentinel.bundle import RunBundle

DEFAULT_STORE = Path("history/flights.jsonl")

# A recurrence older than this is not evidence about the aircraft's current state -- airframes get
# repaired, recalibrated and rebuilt. Callers can widen it; the default is a maintenance interval,
# not a guess.
DEFAULT_WINDOW_DAYS = 30


class FlightRecord(BaseModel):
    """One flight, as history remembers it. Allow-list, not a dump."""

    airframe_id: str
    bundle_id: str
    scenario: str
    created_utc: str
    # Advisory TYPES only, deduplicated. Not times: within-flight ordering is what the judge is
    # being tested on, and leaking a previous flight's ordering would hand it the pattern.
    advisory_types: list[str] = Field(default_factory=list)
    inject_verified: bool = False
    cycles: int = 0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_utc(s: str) -> datetime | None:
    try:
        d = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


class FlightHistory:
    """Append-only cross-flight store, queried by airframe and time window."""

    def __init__(self, path: str | Path = DEFAULT_STORE) -> None:
        self.path = Path(path)

    # ---- writing -------------------------------------------------------------------

    def record_flight(self, bundle: RunBundle, airframe_id: str) -> FlightRecord:
        """Append one bundle's public facts. Never overwrites, never dedupes silently.

        A repeated `bundle_id` is allowed through: the same flight recorded twice is a caller
        bug worth seeing in the data, and silently dropping it would hide a double-count that
        later inflates a recurrence.
        """
        rec = FlightRecord(
            airframe_id=airframe_id,
            bundle_id=bundle.bundle_id,
            scenario=bundle.scenario,
            created_utc=bundle.created_utc or _now_utc().isoformat(timespec="seconds"),
            advisory_types=sorted({a.type for a in bundle.advisories}),
            inject_verified=bundle.inject_verified,
            cycles=len(bundle.cycles),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec.model_dump(mode="json")) + "\n")
        return rec

    # ---- reading -------------------------------------------------------------------

    def _load(self) -> list[FlightRecord]:
        if not self.path.exists():
            return []
        out: list[FlightRecord] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(FlightRecord.model_validate_json(line))
            except Exception:
                # One malformed line must not blind the whole history. Skipping is safe here
                # BECAUSE the aggregate reports how many flights it counted -- a reader can see
                # the number is lower than expected. Raising would make a corrupt line fatal to
                # every future judgement.
                continue
        return out

    def flights_for(self, airframe_id: str,
                    within_days: int = DEFAULT_WINDOW_DAYS,
                    now: datetime | None = None) -> list[FlightRecord]:
        cutoff = (now or _now_utc()) - timedelta(days=within_days)
        out = []
        for r in self._load():
            if r.airframe_id != airframe_id:
                continue
            t = _parse_utc(r.created_utc)
            if t is None or t >= cutoff:
                out.append(r)
        return sorted(out, key=lambda r: r.created_utc)

    def prior_incidents(self, airframe_id: str, exclude_bundle_id: str | None = None,
                        within_days: int = DEFAULT_WINDOW_DAYS,
                        now: datetime | None = None) -> dict[str, Any]:
        """Recurrence summary for one airframe: how often, how recently, out of how many.

        `exclude_bundle_id` leaves the flight under judgement out of its own history. Without
        it the judge sees the current flight's advisories twice, once as evidence and once as
        precedent, which would make every fault look like a recurrence of itself.
        """
        flights = [f for f in self.flights_for(airframe_id, within_days, now)
                   if f.bundle_id != exclude_bundle_id]
        if not flights:
            return {"airframe_id": airframe_id, "flights_in_window": 0,
                    "window_days": within_days,
                    "note": "no earlier flights recorded for this airframe in the window; "
                            "absence of history is not evidence of a healthy airframe"}

        counts: dict[str, int] = defaultdict(int)
        last_seen: dict[str, str] = {}
        for f in flights:
            for t in f.advisory_types:
                counts[t] += 1
                last_seen[t] = max(last_seen.get(t, ""), f.created_utc)

        recurring = {
            t: {"flights_with_it": n,
                "of_flights": len(flights),
                "last_seen_utc": last_seen[t]}
            for t, n in sorted(counts.items(), key=lambda kv: -kv[1])
        }
        return {
            "airframe_id": airframe_id,
            "flights_in_window": len(flights),
            "window_days": within_days,
            "recurring": recurring,
            "note": ("Counts are flights on this airframe that raised each advisory type, "
                     "excluding the flight under judgement. A type recurring across flights "
                     "points at the airframe; one appearing only now points at this flight."),
        }
