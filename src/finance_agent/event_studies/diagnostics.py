"""Estimation and event-window diagnostics. Each check reports a value AND what it threatens.

Thresholds are conventional screening levels, not tests of the study's conclusion.
"""

from __future__ import annotations

import math
from datetime import date

import numpy as np

from finance_agent.event_studies.market_model import MarketModelFit, UnstableEstimation, fit_market_model

OUTLIER_Z = 4.0
BETA_STABILITY_Z = 2.0
LJUNG_BOX_LAGS = 5
EVENT_VOL_RATIO = 4.0


def outliers(residuals: np.ndarray, sessions: list[date], sigma2: float) -> list[dict]:
    if sigma2 <= 0:
        return []
    sd = math.sqrt(sigma2)
    return [
        {"session": str(s), "residual": float(e), "z": float(e / sd)}
        for s, e in zip(sessions, residuals, strict=True)
        if abs(e / sd) > OUTLIER_Z
    ]


def beta_stability(ri: np.ndarray, rm: np.ndarray) -> dict:
    """Split-sample betas (first vs second half of the estimation window)."""
    h = len(ri) // 2
    try:
        a = fit_market_model(ri[:h], rm[:h], min_obs=10)
        b = fit_market_model(ri[h:], rm[h:], min_obs=10)
    except UnstableEstimation as exc:
        return {"status": "NOT_AVAILABLE", "reason": str(exc)}
    z = (a.beta - b.beta) / math.sqrt(a.beta_se**2 + b.beta_se**2)
    return {
        "status": "UNSTABLE" if abs(z) > BETA_STABILITY_Z else "OK",
        "beta_first_half": a.beta,
        "beta_second_half": b.beta,
        "z_difference": z,
        "threshold": BETA_STABILITY_Z,
        "threatens": "parameter stability between the estimation and event windows",
    }


def autocorrelation(residuals: np.ndarray) -> dict:
    from statsmodels.stats.diagnostic import acorr_ljungbox
    from statsmodels.stats.stattools import durbin_watson

    if len(residuals) <= LJUNG_BOX_LAGS + 5:
        return {"status": "NOT_AVAILABLE", "reason": "too few residuals"}
    lb = acorr_ljungbox(residuals, lags=[LJUNG_BOX_LAGS]).iloc[0]
    p = float(lb["lb_pvalue"])
    return {
        "status": "AUTOCORRELATED" if p < 0.05 else "OK",
        "ljung_box_lags": LJUNG_BOX_LAGS,
        "ljung_box_stat": float(lb["lb_stat"]),
        "ljung_box_p_value": p,
        "durbin_watson": float(durbin_watson(residuals)),
        "threatens": "the i.i.d. assumption behind Var(CAR); multi-day CAR standard errors may be biased",
    }


def normality(residuals: np.ndarray) -> dict:
    from statsmodels.stats.stattools import jarque_bera

    if len(residuals) < 20:
        return {"status": "NOT_AVAILABLE", "reason": "too few residuals"}
    jb, p, skew, kurt = (float(v) for v in jarque_bera(residuals))
    return {
        "status": "NON_NORMAL" if p < 0.05 else "OK",
        "jarque_bera": jb,
        "p_value": p,
        "skewness": skew,
        "kurtosis": kurt,
        "threatens": "exactness of the Student-t reference distribution (see placebo check)",
    }


def event_induced_volatility(event_ars: list[float], sigma2: float) -> dict:
    """Mean squared event-window AR relative to the estimation residual variance."""
    vals = [a for a in event_ars if a is not None]
    if len(vals) < 2 or sigma2 <= 0:
        return {"status": "NOT_AVAILABLE", "reason": "needs >= 2 event-window ARs and sigma^2 > 0"}
    ratio = float(np.mean(np.square(vals)) / sigma2)
    return {
        "status": "ELEVATED" if ratio > EVENT_VOL_RATIO else "OK",
        "mean_squared_ar_over_sigma2": ratio,
        "sessions": len(vals),
        "threshold": EVENT_VOL_RATIO,
        "threatens": "Var(CAR) uses the estimation-window variance; event-induced volatility makes "
        "the classical test over-reject (Boehmer, Musumeci & Poulsen 1991)",
    }


def collect_warnings(fit: MarketModelFit | None, diag: dict) -> list[str]:
    out = []
    if diag.get("outliers"):
        out.append(f"{len(diag['outliers'])} estimation residual(s) beyond |z| > {OUTLIER_Z:g}")
    for key, bad in (
        ("beta_stability", "UNSTABLE"),
        ("autocorrelation", "AUTOCORRELATED"),
        ("normality", "NON_NORMAL"),
        ("event_induced_volatility", "ELEVATED"),
    ):
        d = diag.get(key) or {}
        if d.get("status") == bad:
            out.append(f"{key}: {bad} ({d.get('threatens')})")
    if fit is not None and fit.r_squared < 0.05:
        out.append(
            f"low market-model R-squared ({fit.r_squared:.3f}): the benchmark explains little variation"
        )
    return out
