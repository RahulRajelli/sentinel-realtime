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

# How much of an evidence series `detector_evidence` returns verbatim before summarising. Head is
# larger than tail on purpose: onset ordering is the judgement E4 measures, and it lives in the
# first few samples. Named constants rather than literals so a future table can state the value
# the numbers were produced under -- see the docstring for what forced the cap.
EVIDENCE_HEAD = 10
EVIDENCE_TAIL = 5


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

        **Bounded, and it has to be.** This returned every row for every cycle until 2026-08-14,
        when it was measured at 1,056 rows / 191,465 chars (~48k tokens) for `accel_clipping` on
        a 45 s flight -- against a 711-char `summarize()`. One call was ~270x the entire starting
        context, so B3 tripped its token ceiling and degraded to B0 before reasoning at all. The
        agent tier could not be measured, and B2's matched-spend `k` was being derived from that
        meaningless number.

        A detector re-firing 1,056 times is one fact, not 1,056 facts, so the cap costs no
        information a judge could use: onset (`first`), persistence (`last`) and the distribution
        (`by_metric`) are all retained, and `n_rows` states what was elided. `signal_window` on
        the same page already promises "Never the raw series"; this is that rule applied here.

        Small results are returned as a bare list exactly as before -- the shape only changes
        when it would otherwise be unaffordable, and `truncated` says so explicitly.
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
        if len(rows) <= EVIDENCE_HEAD + EVIDENCE_TAIL:
            return rows

        by_metric: dict[str, dict[str, Any]] = {}
        for r in rows:
            g = by_metric.setdefault(r["metric"], {
                "n": 0, "t_first": r["t"], "t_last": r["t"],
                "value_min": r["value"], "value_max": r["value"], "_sum": 0.0,
                "threshold": r["threshold"], "unit": r["unit"],
            })
            g["n"] += 1
            g["t_last"] = r["t"]
            v = r["value"]
            if isinstance(v, (int, float)):
                g["value_min"] = min(g["value_min"], v)
                g["value_max"] = max(g["value_max"], v)
                g["_sum"] += v
        for g in by_metric.values():
            n = g.pop("n")
            total = g.pop("_sum")
            g["n"] = n
            g["value_mean"] = round(total / n, 4) if n else None

        return {
            "incident_type": incident_type,
            "n_rows": len(rows),
            "truncated": True,
            "note": (f"{len(rows)} evidence rows; showing the first {EVIDENCE_HEAD} and last "
                     f"{EVIDENCE_TAIL}. A detector re-firing every cycle repeats one fact -- "
                     f"`by_metric` carries the full distribution, so onset time in `first` is "
                     f"still the earliest sample recorded."),
            "by_metric": by_metric,
            "first": rows[:EVIDENCE_HEAD],
            "last": rows[-EVIDENCE_TAIL:],
        }

    def evidence_untimed(self, incident_type: str) -> Any:
        """`detector_evidence` with every temporal field removed. Opt-in (`OPTIONAL_SPECS`).

        Built from the E4 ablation, 2026-08-14. Two things were established there:

        * B3's symptom-as-root count went 9 -> 7 -> 0 as ordering TOOLS were removed, so the
          error mode tracks the agent's ability to re-query timing;
        * removing those tools also removed the evidence values and accuracy collapsed
          (13 misses, flip rate 0.89, `stiff_airframe` 1.00 -> 0.61). Evidence is load-bearing.

        Both travelled through the same tool, so neither could be isolated. This separates them:
        the values and thresholds stay, `t` does not.

        Note what is deliberately NOT changed. `summarize()` still reports advisories with
        `t_first`, so this judge sees the same ordering B1 sees -- and B1, which has it, scores
        0.89 with zero symptom-as-root errors. The hypothesis under test is that *querying and
        elaborating* the ordering is what does the damage, not knowing it. Hiding it from the
        summary too would test a different and less interesting claim, and would break parity
        with B1.

        Rows are aggregated per metric rather than listed, because a per-row list is a timeline
        with the clock filed off -- position in the list still encodes order.
        """
        by_metric: dict[str, dict[str, Any]] = {}
        severities: set[str] = set()
        n_rows = 0
        for cycle in self._b.cycles:
            for inc in cycle.incidents:
                if inc.type != incident_type:
                    continue
                severities.add(inc.severity)
                for ev in inc.evidence:
                    n_rows += 1
                    g = by_metric.setdefault(ev.metric, {
                        "n": 0, "min": ev.value, "max": ev.value, "_sum": 0.0,
                        "threshold": ev.threshold, "unit": ev.unit,
                        "description": ev.description,
                    })
                    g["n"] += 1
                    if isinstance(ev.value, (int, float)):
                        g["min"] = min(g["min"], ev.value)
                        g["max"] = max(g["max"], ev.value)
                        g["_sum"] += ev.value

        if not by_metric:
            return {"error": f"{incident_type!r} was never detected in this flight",
                    "detected_types": sorted(self._types())}

        for g in by_metric.values():
            total = g.pop("_sum")
            g["mean"] = round(total / g["n"], 4) if g["n"] else None
            if isinstance(g.get("threshold"), (int, float)) and isinstance(g["max"], (int, float)):
                # How far past the line it went, which is the judgement-relevant quantity once
                # the clock is gone.
                g["peak_over_threshold"] = round(g["max"] - g["threshold"], 4)

        return {
            "incident_type": incident_type,
            "severities_seen": sorted(severities),
            "samples": n_rows,
            "by_metric": by_metric,
            "note": ("Timestamps and ordering are omitted from this tool by design. Decide the "
                     "root cause from which measurement breached which threshold, and by how "
                     "much. To cite this evidence, give the metric name and one of the VALUES "
                     "shown here (min, max or mean) and leave `t` null -- a value anchor is "
                     "checked against the recording exactly as a timestamp would be."),
        }

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

    # THE DEFAULT TOOL SURFACE, changed 2026-08-14 on measurement.
    #
    # This is exactly the configuration measured at accuracy 0.96 / compass 0.89 / 5,239 tok,
    # not a set assembled by taste. The previous five-tool default measured 0.67 / 0.00 / 9,805
    # and named a symptom as the root cause in 9 of 27 judgements.
    #
    # The four time-bearing tools moved to OPTIONAL_SPECS rather than being deleted: they are
    # what the ablation manipulates, so the experiment must still be able to offer them. They are
    # not defaults because every configuration containing them scored worse.
    #
    # Ordering is NOT hidden from the judge -- `summarize()` still lists advisories with
    # `t_first`, exactly as B1 sees them. What the agent no longer has is a way to re-query and
    # elaborate the order, which is the thing the ablation showed does the damage.
    SPECS: list[dict[str, Any]] = [
        {"name": "evidence_untimed",
         "description": ("Measured values and thresholds behind one incident type, with no "
                         "timestamps and no ordering. Judge what the evidence says, not when "
                         "it arrived."),
         "parameters": {"type": "object", "properties": {
             "incident_type": {"type": "string"}}, "required": ["incident_type"]}},
        {"name": "get_param",
         "description": "One vehicle parameter value as captured at flight time.",
         "parameters": {"type": "object", "properties": {
             "name": {"type": "string"}}, "required": ["name"]}},
    ]

    # The time-bearing tools. Retired from the default surface 2026-08-14, kept offerable so the
    # ablation that retired them can still be reproduced (`--offer-tools`).
    #
    # Each measured WORSE as a default. Retained rather than deleted because a result nobody can
    # re-run is not a result, and because a future detector set with different semantics might
    # justify revisiting them -- with a measurement, as these were.
    OPTIONAL_SPECS: list[dict[str, Any]] = [
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
    ]

    @classmethod
    def all_specs(cls) -> list[dict[str, Any]]:
        return cls.SPECS + cls.OPTIONAL_SPECS

    def call(self, name: str, arguments: dict[str, Any] | None = None) -> Any:
        """Dispatch one tool call. Unknown names and bad arguments come back as data."""
        args = dict(arguments or {})
        fn = getattr(self, name, None)
        if name not in {s["name"] for s in self.all_specs()} or fn is None:
            return {"error": f"unknown tool {name!r}",
                    "available": [s["name"] for s in self.all_specs()]}
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
