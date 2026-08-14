"""Read-only query surface over a captured flight (Phase E4).

Root-cause vs. symptom discrimination reduces to two questions an engineer asks in front of a
log: **what fired first**, and **does the recorded state explain it**. `ordering()` answers the
first; `detector_evidence()`, `signal_window()` and `get_param()` answer the second.

Three rules, each of which exists because breaking it would corrupt a measurement rather than
merely annoy a caller:

  * **Errors are returned as data, never raised.** A missing signal yields ``{"error": ...}`` so
    the agent can recover and try something else. A raised exception would kill the loop and be
    scored as a MODEL failure when it is a HARNESS one -- an attribution bug, not a crash bug.
  * **`signal_window` returns summary statistics, never a raw dump.** *Less Is More for Monitors*
    found filtered excerpts beat full traces for detecting deviation, and a dump would exhaust
    the token ceiling on one call. `capture.py` does not even store the raw buffer, so this is
    structural rather than a policy the caller could talk its way around.
  * **Nothing here reveals the ground truth.** `expected_root_cause` and `expected_symptoms`
    live in the bundle because a bundle must be self-contained and independently scoreable --
    but a judge that could read them would score 100% and measure nothing. `summarize()` and
    every tool below are built by allow-list for that reason, and a test asserts the label never
    appears in what a judge sees.

Read-only is structural, not promised: the bundle is a frozen file with a content hash, so
there is no write path to expose. Spec section 2.2's zero-write rule holds by construction.
"""

from __future__ import annotations

from typing import Any

from sentinel.bundle import RunBundle

# Bundle FIELD NAMES a judge may never see. Asserted in tests rather than trusted -- the leak
# this prevents would inflate every accuracy number in the published table while nothing failed.
#
# Deliberately field names, not words: the tools return their own explanatory "note" keys, and
# banning the word would ban a useful one while catching nothing. The bundle's own `note` and
# `scenario` also describe the answer ("ambiguous pair A: ... the symptom leads the cause"), so
# they are excluded by the allow-list in `summarize()` and checked by value in the tests.
FORBIDDEN_KEYS = ("expected_root_cause", "expected_symptoms")


class BundleTools:
    """The whole world a judge is allowed to observe, for one bundle."""

    def __init__(self, bundle: RunBundle) -> None:
        self._b = bundle

    # ---- tools ----------------------------------------------------------------------

    def list_advisories(self) -> list[dict[str, Any]]:
        """Every advisory the escalation gate raised, earliest first.

        Pre-injection advisories are included and labelled rather than filtered out: on a real
        aircraft nobody knows where "injection" was, and hiding them would hand the judge a
        clean split that production would not have.
        """
        out: list[dict[str, Any]] = []
        seen: dict[str, dict[str, Any]] = {}
        for a in sorted(self._b.advisories, key=lambda x: x.t):
            if a.type in seen:
                seen[a.type]["count"] += 1
                continue
            rec = {"type": a.type, "severity": a.severity, "t_first": round(a.t, 3),
                   "reason": a.reason, "before_any_change": a.pre_inject, "count": 1}
            seen[a.type] = rec
            out.append(rec)
        return out

    def ordering(self, type_a: str, type_b: str) -> dict[str, Any]:
        """Which of two incident types was detected first, and by how long.

        Uses first *incident* time rather than first *advisory* time. The gate can collapse two
        incidents raised in one cycle into an ordering decided by detector registration order
        (`runner.py:39`), which is arbitrary; the incident timeline is what physically happened.
        """
        ta, tb = self._first_incident_t(type_a), self._first_incident_t(type_b)
        if ta is None and tb is None:
            return {"error": f"neither {type_a!r} nor {type_b!r} was detected in this flight"}
        if ta is None:
            return {"first": type_b, "delta_s": None,
                    "note": f"{type_a!r} was never detected"}
        if tb is None:
            return {"first": type_a, "delta_s": None,
                    "note": f"{type_b!r} was never detected"}
        if ta == tb:
            return {"first": None, "delta_s": 0.0,
                    "note": "both first detected in the same cycle; this flight cannot "
                            "separate them in time"}
        first, delta = (type_a, tb - ta) if ta < tb else (type_b, ta - tb)
        return {"first": first, "delta_s": round(abs(delta), 3),
                f"t_{type_a}": round(ta, 3), f"t_{type_b}": round(tb, 3)}

    def detector_evidence(self, incident_type: str) -> Any:
        """The measured values and thresholds behind one incident type.

        This is what makes a verdict citable: an advisory is only checkable if the reader can
        see which metric crossed which threshold, and by how much.
        """
        rows: list[dict[str, Any]] = []
        for cycle in self._b.cycles:
            for inc in cycle.incidents:
                if inc.type != incident_type:
                    continue
                for ev in inc.evidence:
                    rows.append({
                        "t": round(cycle.t, 3), "metric": ev.metric,
                        "value": ev.value, "threshold": ev.threshold,
                        "unit": ev.unit, "description": ev.description,
                        "severity": inc.severity,
                    })
        if not rows:
            return {"error": f"{incident_type!r} was never detected in this flight",
                    "detected_types": sorted(self._types())}
        return rows

    def signal_window(self, metric: str, t0: float, t1: float) -> dict[str, Any]:
        """Summary statistics for one metric over a time window. Never the raw series.

        Samples come from recorded detector evidence, which is what the bundle stores. So a
        metric only has values at moments a detector was already concerned about it -- stated
        here because a judge reading `n=3` should not conclude the sensor was sampled 3 times.
        """
        if t1 < t0:
            return {"error": f"window end {t1} precedes start {t0}"}
        vals: list[tuple[float, float]] = []
        for cycle in self._b.cycles:
            if not (t0 <= cycle.t <= t1):
                continue
            for inc in cycle.incidents:
                for ev in inc.evidence:
                    if ev.metric == metric:
                        vals.append((cycle.t, ev.value))
        if not vals:
            return {"error": f"no samples of {metric!r} in [{t0}, {t1}]",
                    "available_metrics": sorted(self._metrics()),
                    "window_of_flight": [self._b.t_start, self._b.t_end]}
        v = [x for _, x in vals]
        return {"metric": metric, "n": len(v), "min": min(v), "max": max(v),
                "mean": round(sum(v) / len(v), 4),
                "first_t": round(vals[0][0], 3), "last_t": round(vals[-1][0], 3),
                "note": "samples are detector-evidence points, not a raw sensor series"}

    def get_param(self, name: str) -> dict[str, Any]:
        """One vehicle parameter as captured at flight time.

        Thresholds come from the aircraft, not from a config file, so a verdict about a
        threshold breach is only interpretable next to the value that was actually loaded.
        """
        if name not in self._b.params:
            near = sorted(k for k in self._b.params if name.split("_")[0] in k)[:8]
            return {"error": f"parameter {name!r} not captured", "similar": near}
        return {"name": name, "value": self._b.params[name]}

    # ---- dispatch -------------------------------------------------------------------

    SPECS: list[dict[str, Any]] = [
        {"name": "list_advisories",
         "description": "Every advisory raised, earliest first, with severity and first-seen time.",
         "parameters": {"type": "object", "properties": {}, "required": []}},
        {"name": "ordering",
         "description": "Which of two incident types was physically detected first, and by how many seconds.",
         "parameters": {"type": "object", "properties": {
             "type_a": {"type": "string"}, "type_b": {"type": "string"}},
             "required": ["type_a", "type_b"]}},
        {"name": "detector_evidence",
         "description": "Measured values and thresholds behind one incident type.",
         "parameters": {"type": "object", "properties": {
             "incident_type": {"type": "string"}}, "required": ["incident_type"]}},
        {"name": "signal_window",
         "description": "Summary statistics (n/min/max/mean) for one metric over a time window.",
         "parameters": {"type": "object", "properties": {
             "metric": {"type": "string"}, "t0": {"type": "number"}, "t1": {"type": "number"}},
             "required": ["metric", "t0", "t1"]}},
        {"name": "get_param",
         "description": "One vehicle parameter value as captured at flight time.",
         "parameters": {"type": "object", "properties": {
             "name": {"type": "string"}}, "required": ["name"]}},
    ]

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Dispatch one tool call. Unknown names and bad arguments come back as data."""
        args = dict(arguments or {})
        fn = getattr(self, name, None)
        if name not in {s["name"] for s in self.SPECS} or fn is None:
            return {"error": f"unknown tool {name!r}",
                    "available": [s["name"] for s in self.SPECS]}
        try:
            return fn(**args)
        except TypeError as exc:
            # Wrong or missing arguments. Returned so the agent can correct itself; raising
            # here would end the run and be misattributed to the model.
            return {"error": f"bad arguments for {name}: {exc}"}

    # ---- the shared starting context -------------------------------------------------

    def summarize(self) -> dict[str, Any]:
        """The context every judge starts from -- B1 and B2 get exactly this, B3 starts here.

        Built by allow-list, not by dumping the bundle and deleting fields: a future field added
        to RunBundle must be opted IN to be visible, so the ground-truth label cannot leak by
        someone forgetting to exclude it. If B3 wins only because it saw more of the file than
        B1, the experiment measured input size.
        """
        return {
            "flight_window_s": [self._b.t_start, self._b.t_end],
            "cycles": len(self._b.cycles),
            "advisories": self.list_advisories(),
            "detected_incident_types": sorted(self._types()),
            "available_metrics": sorted(self._metrics()),
            "params_hash": self._b.params_hash,
            "params_captured": len(self._b.params),
        }

    # ---- internals ------------------------------------------------------------------

    def _types(self) -> set[str]:
        return {inc.type for c in self._b.cycles for inc in c.incidents}

    def _metrics(self) -> set[str]:
        return {ev.metric for c in self._b.cycles for inc in c.incidents for ev in inc.evidence}

    def _first_incident_t(self, incident_type: str) -> float | None:
        for cycle in self._b.cycles:
            if any(inc.type == incident_type for inc in cycle.incidents):
                return cycle.t
        return None
