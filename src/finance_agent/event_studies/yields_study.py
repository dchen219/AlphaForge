"""Treasury yield-change event study (DGS2 / DGS10). Rates, not prices: changes in basis points.

* Observation days are dates with a PUBLISHED value (ALFRED '.' rows = no quote; federal and
  bond-market holidays produce such rows). Windows count observation days, never calendar days,
  and a change spanning a no-quote day is reported as such (never filled).
* Day-0 rule (``yield-close-v1``): H.15 constant-maturity yields come from the Treasury yield curve,
  which uses indicative bid-side quotes collected by the Federal Reserve Bank of New York at or
  near 15:30 ET. A release before 15:30 ET on an observation day is in that day's value
  (day 0 = that day); a release at/after 15:30 ET, or on a no-quote day, falls in the next
  observation day. Date-only releases: day 0 = first observation day on/after the date, and the
  window must contain days 0 and +1.
* Window change over [a, b] = 100 * (y_b - y_{a-1}) basis points (yields in percent).
* There is no expected-return model for yields: the pre-event distribution of same-length changes
  (estimation offsets) is reported as a descriptive reference, not a significance test.
* Daily end-of-day quotes cannot show an intraday reaction.
"""

from __future__ import annotations

from datetime import date, datetime, time

import numpy as np

from finance_agent.event_studies.engine import SCOPE, origin_of, window_timing_ok
from finance_agent.event_studies.schemas import (
    DataMode,
    EventAlignment,
    EventStudySpecification,
    StudyStatus,
    WindowStatus,
)
from finance_agent.market.store import MarketStore
from finance_agent.market.universe import YIELD_SERIES
from finance_agent.timeutil import ET

YIELD_QUOTE_TIME = time(15, 30)


def yield_day0(alignment: EventAlignment, obs_days: list[date]) -> tuple[date | None, str]:
    if alignment.local_date is None:
        return None, "no publication date"
    d = alignment.local_date
    after = [x for x in obs_days if x >= d]
    if not after:
        return None, f"no yield observation on or after {d}"
    if alignment.event_time is None:  # date only
        return after[0], "date-only release: day 0 = first yield observation on/after the publication date"
    local = alignment.event_time.astimezone(ET)
    if after[0] == d and local.time() < YIELD_QUOTE_TIME:
        return d, "released before the ~15:30 ET quote time: day 0 = same observation day"
    nxt = [x for x in obs_days if x > d]
    if not nxt:
        return None, f"no yield observation after {d}"
    why = "at/after the ~15:30 ET quote time" if after[0] == d else "on a day without a yield quote"
    return nxt[0], f"released {why}: day 0 = next observation day {nxt[0]}"


def run_yield_study(
    spec: EventStudySpecification,
    alignment: EventAlignment,
    store: MarketStore,
    *,
    event_available_at: datetime | None,
    event_origin: str,
) -> dict:
    sid = spec.target.upper()
    out = {
        "kind": "yield",
        "series_id": sid,
        "unit": "basis_points",
        "spec_hash": spec.spec_hash(),
        "alignment": alignment.model_dump(mode="json"),
        "interpretation_scope": {
            **SCOPE,
            "B_benchmark_adjusted": "not applicable: yield changes are reported without an expected-change model",
            "C_statistical_significance": "not tested; the pre-event distribution of same-length changes "
            "is a descriptive reference only",
        },
        "warnings": ["daily end-of-day yields (~15:30 ET quotes): NOT an intraday reaction"],
    }
    if sid not in YIELD_SERIES:
        out.update(status=StudyStatus.FAILED, status_reason=f"unsupported yield series {sid}")
        return out
    if not alignment.eligible:
        out.update(status=StudyStatus.INELIGIBLE, status_reason=alignment.rule)
        return out
    pit = spec.data_mode == DataMode.POINT_IN_TIME
    if pit and (event_available_at is None or event_available_at > spec.as_of):
        out.update(status=StudyStatus.INELIGIBLE, status_reason="event not available at the decision cutoff")
        return out
    rows = store.yields(sid, as_of=spec.as_of if pit else None)
    published = [(r["observation_date"], r["value"]) for r in rows if r["value"] is not None]
    no_quote = sorted(r["observation_date"] for r in rows if r["value"] is None)
    obs_days = [d for d, _ in published]
    level = dict(published)
    day0, rule = yield_day0(alignment, obs_days)
    origins = sorted({r["data_origin"] for r in rows})
    origin, synth = origin_of([event_origin, *origins])
    out.update(data_origin=origin, synthetic=synth, day0=str(day0) if day0 else None, day0_rule=rule)
    if day0 is None:
        out.update(status=StudyStatus.INSUFFICIENT_DATA, status_reason=rule)
        return out
    i0 = obs_days.index(day0)
    est = spec.estimation_window
    lo = min(est.start, *(w.start for w in spec.event_windows)) - 1
    hi = max(w.end for w in spec.event_windows)
    if i0 + lo < 0:
        out.update(
            status=StudyStatus.INSUFFICIENT_DATA,
            status_reason=f"stored {sid} observations do not cover the estimation offsets from {lo} around {day0}",
        )
        return out
    top = min(hi, len(obs_days) - 1 - i0)  # last offset with a published (and, PIT, available) value
    day = {k: obs_days[i0 + k] for k in range(lo, top + 1)}
    daily = [
        {
            "relative_day": k,
            "date": str(day[k]),
            "yield_pct": level[day[k]],
            "change_bp": round(100.0 * (level[day[k]] - level[day[k - 1]]), 10),
            "previous_date": str(day[k - 1]),
            "no_quote_days_between": [str(x) for x in no_quote if day[k - 1] < x < day[k]],
        }
        for k in range(min(w.start for w in spec.event_windows), top + 1)
    ]
    est_changes = np.array(
        [100.0 * (level[day[k]] - level[day[k - 1]]) for k in range(est.start, est.end + 1)], float
    )
    windows = []
    for w in spec.event_windows:
        ok, why = window_timing_ok(w, alignment)
        if w.end > top:
            windows.append({"window": w.label, "start": w.start, "end": w.end, "status": WindowStatus.OUT_OF_RANGE,
                            "reason": f"no {sid} observation for offset +{w.end} yet (last available {day[top]}); "
                            "the window is not shortened"})  # fmt: skip
            continue
        row = {"window": w.label, "start": w.start, "end": w.end,
               "dates": [str(day[w.start]), str(day[w.end])]}  # fmt: skip
        if not ok:
            windows.append({**row, "status": WindowStatus.INELIGIBLE_TIMING, "reason": why})
            continue
        before, last = level[day[w.start - 1]], level[day[w.end]]
        change = round(100.0 * (last - before), 10)
        # pre-event reference: all same-length changes inside the estimation offsets (overlapping)
        L = w.length
        ref = [100.0 * (level[day[k + L - 1]] - level[day[k - 1]]) for k in range(est.start, est.end - L + 2)]
        ref_arr = np.array(ref, float)
        windows.append(
            {
                **row,
                "status": WindowStatus.VALID,
                "yield_before_pct": before,
                "yield_before_date": str(day[w.start - 1]),
                "yield_after_pct": last,
                "change_bp": change,
                "no_quote_days_inside": [
                    x
                    for d in daily
                    if w.start <= d["relative_day"] <= w.end
                    for x in d["no_quote_days_between"]
                ],
                "pre_event_reference": {
                    "method": f"all {len(ref)} overlapping {L}-day changes over estimation offsets "
                    f"[{est.start},{est.end}] (descriptive; overlapping windows are dependent)",
                    "mean_bp": float(ref_arr.mean()),
                    "sd_bp": float(ref_arr.std(ddof=1)) if len(ref) > 1 else None,
                    "share_with_larger_absolute_change": float(np.mean(np.abs(ref_arr) >= abs(change)))
                    if len(ref)
                    else None,
                },
            }
        )
    valid = any(w["status"] == WindowStatus.VALID for w in windows)
    out.update(
        status=StudyStatus.COMPLETED if valid else StudyStatus.INSUFFICIENT_DATA,
        status_reason=None if valid else "no yield window has complete observations",
        daily_changes=daily,
        windows=windows,
        estimation_reference={
            "offsets": [est.start, est.end],
            "daily_change_sd_bp": float(est_changes.std(ddof=1)),
            "observations": len(est_changes),
        },
        data_manifest={
            "series_id": sid,
            "mode": str(spec.data_mode),
            "as_of": spec.as_of.isoformat() if spec.as_of else None,
            "vintages": sorted(
                {str(r["vintage_date"]) for r in rows if day[lo] <= r["observation_date"] <= day[top]}
            ),
            "versions": sorted({r["version"] for r in rows}),
            "data_origins": origins,
            "statement": "latest stored vintage of each observation (research)"
            if not pit
            else "only vintages available at the cutoff (point-in-time)",
        },
    )
    return out
