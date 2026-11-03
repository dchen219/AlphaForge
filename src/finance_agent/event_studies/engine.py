"""Single-event equity/ETF study: alignment -> windows -> returns -> model -> CAR -> inference.

Deterministic and side-effect free apart from reading the market store. Nothing is filled:
missing sessions are reported, never interpolated, and a window with a missing observation is
invalid (never silently shortened).
"""

from __future__ import annotations

import math
from datetime import datetime

import numpy as np

from finance_agent.errors import DataUnavailableError
from finance_agent.event_studies import diagnostics as dg
from finance_agent.event_studies.inference import (
    market_adjusted_inference,
    market_model_inference,
    placebo_distribution,
)
from finance_agent.event_studies.market_model import (
    MarketAdjustedFit,
    MarketModelFit,
    UnstableEstimation,
    fit_market_adjusted,
    fit_market_model,
)
from finance_agent.event_studies.returns import ReturnSeries, fetch_returns
from finance_agent.event_studies.schemas import (
    AbnormalReturnObservation,
    DataMode,
    EstimationDiagnostics,
    EventAlignment,
    EventStudyResult,
    EventStudySpecification,
    EventWindow,
    ExpectedReturnModel,
    Inference,
    InferenceStatus,
    ReturnType,
    StudyStatus,
    TimingClass,
    WindowResult,
    WindowStatus,
)
from finance_agent.event_studies.windows import grid_bounds, session_grid
from finance_agent.market.calendar import TradingCalendar
from finance_agent.market.service import MarketDataService
from finance_agent.timeutil import available_at

SCOPE = {
    "A_descriptive": "observed price movement around the event (raw returns): description only",
    "B_benchmark_adjusted": "abnormal returns relative to the stated benchmark model: an association, "
    "not an effect",
    "C_statistical_significance": "a classical test of whether the CAR is unusual relative to the "
    "estimation-window noise, under the stated assumptions",
    "D_causal_identification": "NOT ESTABLISHED. A significant abnormal return in the same window does "
    "not show that this event caused it (confounders, timing, model misspecification).",
}


def origin_of(origins: list[str]) -> tuple[str, bool]:
    """Synthetic dominates: any synthetic input makes the whole result synthetic."""
    flat = sorted({o for o in origins if o})
    synthetic = any("synthetic" in o for o in flat)
    if synthetic:
        return "fixture:synthetic", True
    if not flat:
        return "unknown", False
    return (flat[0] if len(flat) == 1 else "mixed:" + "+".join(flat)), False


def window_timing_ok(w: EventWindow, alignment: EventAlignment) -> tuple[bool, str | None]:
    if alignment.timing_uncertain and not (w.start <= 0 and w.end >= 1):
        return False, (
            "publication time is uncertain (date only): the window must contain day 0 and day +1, "
            "because the release may have followed the day-0 close"
        )
    return True, None


def _compound(values: list[float], rt: ReturnType) -> float:
    if rt == ReturnType.LOG:
        return math.exp(sum(values)) - 1.0
    total = 1.0
    for v in values:
        total *= 1.0 + v
    return total - 1.0


def run_equity_study(
    spec: EventStudySpecification,
    alignment: EventAlignment,
    *,
    market: MarketDataService,
    calendar: TradingCalendar,
    event_available_at: datetime | None,
    event_origin: str,
) -> EventStudyResult:
    sh = spec.spec_hash()
    base = {"spec_hash": sh, "alignment": alignment, "interpretation_scope": SCOPE}

    def stop(status: StudyStatus, reason: str, **kw) -> EventStudyResult:
        origin, synth = origin_of([event_origin, *kw.pop("origins", [])])
        return EventStudyResult(
            status=status, status_reason=reason, data_origin=origin, synthetic=synth, **base, **kw
        )

    if not alignment.eligible or alignment.day0 is None:
        return stop(StudyStatus.INELIGIBLE, alignment.rule)
    pit = spec.data_mode == DataMode.POINT_IN_TIME
    if pit and (event_available_at is None or event_available_at > spec.as_of):
        return stop(StudyStatus.INELIGIBLE, "the event was not yet available at the decision cutoff")

    est, windows = spec.estimation_window, spec.event_windows
    lo, hi = grid_bounds(est, windows)
    try:
        grid = session_grid(calendar, alignment.day0, lo, hi)
    except DataUnavailableError as exc:
        return stop(StudyStatus.INSUFFICIENT_DATA, str(exc))
    sessions = [grid[k] for k in range(lo, hi + 1)]
    off_of = {d: k for k, d in grid.items()}
    needs_bench = spec.model != ExpectedReturnModel.RAW
    target = fetch_returns(
        market, spec.target, sessions, basis=spec.price_basis, return_type=spec.return_type,
        mode=spec.data_mode, as_of=spec.as_of,
    )  # fmt: skip
    bench: ReturnSeries | None = None
    if spec.benchmark is not None:
        bench = fetch_returns(
            market, spec.benchmark.instrument_id, sessions, basis=spec.price_basis,
            return_type=spec.return_type, mode=spec.data_mode, as_of=spec.as_of,
        )  # fmt: skip
    manifest = {"target": target.manifest, "benchmark": bench.manifest if bench else None}
    origins = [*target.data_origins, *(bench.data_origins if bench else [])]
    if "error" in target.manifest or (needs_bench and bench is not None and "error" in bench.manifest):
        err = target.manifest.get("error") or (bench.manifest.get("error") if bench else "")
        return stop(StudyStatus.INSUFFICIENT_DATA, err, data_manifest=manifest, origins=origins)

    # ---------------- estimation sample (paired, valid, never filled)
    est_offsets = list(range(est.start, est.end + 1))
    pairs, missing = [], []
    excluded = set(spec.excluded_estimation_sessions)
    for k in est_offsets:
        d = grid[k]
        ri = target.get(d)
        rm = bench.get(d) if bench else None
        if d in excluded:
            missing.append(
                {"session": str(d), "offset": k, "reason": "excluded by specification (robustness)"}
            )
        elif ri is None:
            missing.append({"session": str(d), "offset": k, "reason": f"{spec.target}: {target.reason(d)}"})
        elif needs_bench and rm is None:
            missing.append({"session": str(d), "offset": k, "reason": f"benchmark: {bench.reason(d)}"})
        else:
            pairs.append((k, d, ri, rm))
    diag = EstimationDiagnostics(
        expected_sessions=len(est_offsets), valid_observations=len(pairs), missing_sessions=missing
    )
    if pit:
        late = [d for d in sessions if off_of[d] <= est.end and available_at(d) > spec.as_of]
        if late:
            return stop(
                StudyStatus.INSUFFICIENT_DATA,
                f"{len(late)} estimation session(s) were not yet final at the cutoff",
                data_manifest=manifest, origins=origins, estimation=diag,
            )  # fmt: skip
    fit: MarketModelFit | None = None
    fit_ma: MarketAdjustedFit | None = None
    est_error = None
    ri_arr = np.array([p[2] for p in pairs], float)
    rm_arr = np.array([p[3] if p[3] is not None else np.nan for p in pairs], float)
    try:
        if spec.model == ExpectedReturnModel.MARKET_MODEL:
            fit = fit_market_model(ri_arr, rm_arr, est.min_observations)
        elif spec.model == ExpectedReturnModel.MARKET_ADJUSTED:
            fit_ma = fit_market_adjusted(ri_arr, rm_arr, est.min_observations)
    except UnstableEstimation as exc:
        est_error = str(exc)
        diag.status = "INSUFFICIENT_DATA" if "observations" in est_error else "UNSTABLE"
        diag.warnings.append(est_error)
    est_sessions = [p[1] for p in pairs]
    est_offs = [p[0] for p in pairs]
    if fit is not None:
        diag.alpha, diag.beta, diag.alpha_se, diag.beta_se = fit.alpha, fit.beta, fit.alpha_se, fit.beta_se
        diag.residual_variance, diag.residual_df, diag.r_squared = fit.sigma2, fit.df, fit.r_squared
        diag.benchmark_variance, diag.condition_number = fit.benchmark_variance, fit.condition_number
        diag.outliers = dg.outliers(fit.residuals, est_sessions, fit.sigma2)
        diag.beta_stability = dg.beta_stability(ri_arr, rm_arr)
        diag.autocorrelation = dg.autocorrelation(fit.residuals)
        diag.normality = dg.normality(fit.residuals)
    elif fit_ma is not None:
        diag.residual_variance, diag.residual_df = fit_ma.sigma2, fit_ma.df
        diag.outliers = dg.outliers(fit_ma.residuals - fit_ma.mean, est_sessions, fit_ma.sigma2)
        diag.autocorrelation = dg.autocorrelation(fit_ma.residuals - fit_ma.mean)
        diag.normality = dg.normality(fit_ma.residuals - fit_ma.mean)

    # ---------------- event-window observations
    ev_lo, ev_hi = min(w.start for w in windows), max(w.end for w in windows)
    obs: dict[int, AbnormalReturnObservation] = {}
    for k in range(ev_lo, ev_hi + 1):
        d = grid[k]
        ri, rm = target.get(d), (bench.get(d) if bench else None)
        exp = ar = var = None
        note = None
        if ri is None:
            note = f"{spec.target}: {target.reason(d)}"
        elif needs_bench and rm is None:
            note = f"benchmark: {bench.reason(d)}"
        if pit and available_at(d) > spec.as_of:
            note = "bar not final at the decision cutoff"
            ri = rm = None
        if ri is not None:
            if fit is not None and rm is not None:
                exp = fit.alpha + fit.beta * rm
                ar = ri - exp
                var = float(fit.ar_variance(np.array([rm]))[0])
            elif fit_ma is not None and rm is not None:
                exp, ar, var = rm, ri - rm, fit_ma.sigma2
            elif spec.model == ExpectedReturnModel.RAW:
                ar = None
        obs[k] = AbnormalReturnObservation(
            relative_session=k, session=d, asset_return=ri, benchmark_return=rm,
            expected_return=exp, abnormal_return=ar, abnormal_return_variance=var, note=note,
        )  # fmt: skip
    if fit is not None or fit_ma is not None:
        sigma2 = fit.sigma2 if fit is not None else fit_ma.sigma2
        diag.event_induced_volatility = dg.event_induced_volatility(
            [o.abnormal_return for o in obs.values() if o.relative_session >= 0], sigma2
        )
    diag.warnings += dg.collect_warnings(fit, diag.model_dump())

    # ---------------- windows
    results = []
    for w in windows:
        offs = list(range(w.start, w.end + 1))
        wsessions = [grid[k] for k in offs]
        ok, why = window_timing_ok(w, alignment)
        if not ok:
            results.append(
                WindowResult(window=w.label, start=w.start, end=w.end, sessions=wsessions,
                             status=WindowStatus.INELIGIBLE_TIMING, reason=why)
            )  # fmt: skip
            continue
        if pit and any(available_at(d) > spec.as_of for d in wsessions):
            results.append(
                WindowResult(window=w.label, start=w.start, end=w.end, sessions=wsessions,
                             status=WindowStatus.NOT_AVAILABLE_AT_CUTOFF,
                             reason="the window's last daily bar was not final at the decision cutoff")
            )  # fmt: skip
            continue
        bad = [
            obs[k]
            for k in offs
            if obs[k].asset_return is None or (needs_bench and obs[k].benchmark_return is None)
        ]
        if bad:
            results.append(
                WindowResult(
                    window=w.label, start=w.start, end=w.end, sessions=wsessions,
                    status=WindowStatus.MISSING_OBSERVATIONS,
                    reason="; ".join(f"{o.session} ({o.relative_session:+d}): {o.note}" for o in bad),
                )
            )  # fmt: skip
            continue
        ri_w = [obs[k].asset_return for k in offs]
        wr = WindowResult(
            window=w.label, start=w.start, end=w.end, sessions=wsessions, status=WindowStatus.VALID,
            raw_cumulative_return=_compound(ri_w, spec.return_type), sum_asset_return=float(sum(ri_w)),
        )  # fmt: skip
        if bench is not None and all(obs[k].benchmark_return is not None for k in offs):
            wr.benchmark_cumulative_return = _compound(
                [obs[k].benchmark_return for k in offs], spec.return_type
            )
            wr.bhar = wr.raw_cumulative_return - wr.benchmark_cumulative_return
        if spec.model == ExpectedReturnModel.RAW:
            wr.inference = Inference(
                status=InferenceStatus.NOT_APPLICABLE,
                warnings=["raw returns are descriptive; no benchmark adjustment and no test"],
            )
        elif est_error is not None:
            wr.inference = Inference(status=InferenceStatus.NOT_AVAILABLE, warnings=[est_error])
        else:
            wr.car = float(sum(obs[k].abnormal_return for k in offs))
            rm_w = np.array([obs[k].benchmark_return for k in offs], float)
            if fit is not None:
                inf = market_model_inference(fit, wr.car, rm_w)
                resid = fit.residuals
            else:
                inf = market_adjusted_inference(fit_ma, wr.car, w.length)
                resid = fit_ma.residuals
            inf.placebo = placebo_distribution(resid, est_offs, w.length, wr.car)
            _limit(inf, diag, alignment, w)
            wr.inference = inf
        results.append(wr)

    origin, synth = origin_of([event_origin, *origins])
    status, reason = StudyStatus.COMPLETED, None
    if est_error is not None and spec.model != ExpectedReturnModel.RAW:
        status, reason = StudyStatus.INSUFFICIENT_DATA, est_error
    elif not any(r.status == WindowStatus.VALID for r in results):
        status, reason = StudyStatus.INSUFFICIENT_DATA, "no event window has complete observations"
    if pit and status == StudyStatus.COMPLETED:
        unverified = [
            m["instrument_id"] for m in (target.manifest, bench.manifest if bench else None)
            if m and m.get("point_in_time_verified") is False
        ]  # fmt: skip
        if unverified:
            status = StudyStatus.UNVERIFIED
            reason = (
                f"point-in-time availability of {', '.join(unverified)} bars rests on the backfill "
                "assumption (bars were first retrieved after the cutoff); not verified as contemporaneous"
            )
    warnings = list(target.notes) + (list(bench.notes) if bench else [])
    if alignment.contamination_warning:
        warnings.append(alignment.contamination_warning)
    warnings.append("daily close-to-close returns: NOT an immediate post-announcement (intraday) response")
    return EventStudyResult(
        status=status, status_reason=reason, estimation=diag,
        observations=[obs[k] for k in sorted(obs)], windows=results, data_manifest=manifest,
        data_origin=origin, synthetic=synth, warnings=warnings, **base,
    )  # fmt: skip


def _limit(inf: Inference, diag: EstimationDiagnostics, alignment: EventAlignment, w: EventWindow) -> None:
    """Downgrade inference to LIMITED when a diagnostic undermines an assumption it relies on."""
    reasons = []
    if (diag.beta_stability or {}).get("status") == "UNSTABLE":
        reasons.append("unstable split-sample beta")
    if w.length > 1 and (diag.autocorrelation or {}).get("status") == "AUTOCORRELATED":
        reasons.append("autocorrelated residuals (multi-day CAR variance assumes independence)")
    if (diag.event_induced_volatility or {}).get("status") == "ELEVATED":
        reasons.append("event-induced volatility")
    if (diag.normality or {}).get("status") == "NON_NORMAL":
        reasons.append("non-normal residuals (compare the placebo p-value)")
    if alignment.timing_class == TimingClass.REGULAR_SESSION and w.start <= 0 <= w.end:
        reasons.append("day-0 return includes pre-release trading")
    if reasons:
        inf.status = InferenceStatus.LIMITED
        inf.warnings.extend(reasons)


def apply_confounders(result: EventStudyResult, confounders: list[dict]) -> None:
    """Flag inference in windows that contain a confounding event (never drops the window)."""
    for wr in result.windows:
        hits = [
            c for c in confounders
            if c.get("relation") == "confounder" and c.get("relative_session") is not None
            and wr.start <= c["relative_session"] <= wr.end
        ]  # fmt: skip
        if (
            hits
            and wr.inference is not None
            and wr.inference.status in (InferenceStatus.OK, InferenceStatus.LIMITED)
        ):
            wr.inference.status = InferenceStatus.LIMITED
            wr.inference.warnings.append(
                f"{len(hits)} other recorded event(s) in the window: "
                + ", ".join(f"{c['event_subtype']} ({c['relative_session']:+d})" for c in hits[:5])
            )
