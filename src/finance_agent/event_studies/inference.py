"""Classical single-event CAR inference and a pre-event placebo robustness check.

Market model (Campbell, Lo & MacKinlay 1997, ch. 4; MacKinlay 1997):
for an event window of L sessions with benchmark returns R_m* (L-vector) and X* = [1, R_m*],

    Var(AR*)  = sigma^2 (I + X* (X'X)^-1 X*')        (L x L; NOT diagonal)
    Var(CAR)  = iota' Var(AR*) iota = sigma^2 (L + g' (X'X)^-1 g),  g = X*' iota = (L, sum R_m*)

The second term is the estimation error in (alpha_hat, beta_hat); it is common to every event-window
AR, which is why the ARs are correlated and Var(CAR) is not simply L sigma^2. Under H0 (E[CAR] = 0)
with i.i.d. normal disturbances, t = CAR / sqrt(Var(CAR)) ~ Student t with n - 2 degrees of freedom
(sigma^2 is estimated with n - 2 df). The p-value is two-sided; the CI is CAR +/- t_{0.975, n-2} SE.

Market-adjusted model: AR = R_i - R_m involves no estimated parameters, so
Var(CAR) = L sigma_d^2 with sigma_d^2 the estimation-window sample variance of R_i - R_m (n - 1 df).
"""

from __future__ import annotations

import math

import numpy as np

from finance_agent.event_studies.market_model import MarketAdjustedFit, MarketModelFit
from finance_agent.event_studies.schemas import Inference, InferenceStatus

CONFIDENCE = 0.95
MIN_PLACEBO_WINDOWS = 30
# numerically exact fits leave sigma^2 at floating-point noise (~1e-35), not exactly 0
EXACT_FIT_TOL = 1e-12
MIN_VARIANCE = 1e-20

COMMON_ASSUMPTIONS = [
    "estimation-window disturbances are i.i.d. normal with constant variance",
    "the event does not change the variance of returns (no event-induced volatility)",
    "the expected-return model and its parameters are stable from the estimation window to the event window",
    "no other information affecting the asset is released in the event window (see confounder audit)",
    "statistical significance is evidence of an unusual return, NOT causal identification of the event",
]


def _t_test(car: float, var: float, df: int) -> tuple[float, float, float, float, float]:
    from scipy import stats

    se = math.sqrt(var)
    t = car / se
    p = float(2.0 * stats.t.sf(abs(t), df))
    q = float(stats.t.ppf(0.5 + CONFIDENCE / 2.0, df))
    return se, t, p, car - q * se, car + q * se


def car_variance_market_model(fit: MarketModelFit, rm_event: np.ndarray) -> float:
    L = len(rm_event)
    g = np.array([float(L), float(np.sum(rm_event))])
    return float(fit.sigma2 * (L + g @ fit.xtx_inv @ g))


def market_model_inference(fit: MarketModelFit, car: float, rm_event: np.ndarray) -> Inference:
    L = len(rm_event)
    if fit.sigma2 <= 0 or not math.isfinite(fit.sigma2) or (1.0 - fit.r_squared) < EXACT_FIT_TOL:
        return Inference(
            status=InferenceStatus.NOT_AVAILABLE,
            warnings=[
                "(numerically) zero residual variance: the market model fits exactly; a t-statistic is undefined"
            ],
        )
    var = car_variance_market_model(fit, rm_event)
    se, t, p, lo, hi = _t_test(car, var, fit.df)
    return Inference(
        status=InferenceStatus.OK,
        null_hypothesis=f"E[CAR] = 0 over the {L}-session window (no abnormal return)",
        standard_error=se,
        standard_error_ignoring_estimation_error=math.sqrt(fit.sigma2 * L),
        test_statistic=t,
        distribution=f"Student t, {fit.df} df",
        degrees_of_freedom=fit.df,
        p_value=p,
        ci_low=lo,
        ci_high=hi,
        assumptions=[
            *COMMON_ASSUMPTIONS,
            "Var(CAR) includes alpha/beta estimation error and the covariance it induces between event-window ARs",
        ],
    )


def market_adjusted_inference(fit: MarketAdjustedFit, car: float, L: int) -> Inference:
    if fit.sigma2 <= MIN_VARIANCE or not math.isfinite(fit.sigma2):
        return Inference(status=InferenceStatus.NOT_AVAILABLE, warnings=["zero variance of R_i - R_m"])
    se, t, p, lo, hi = _t_test(car, fit.sigma2 * L, fit.df)
    warnings = []
    # The market-adjusted model assumes alpha = 0 and beta = 1; a non-zero estimation mean signals bias.
    mean_t = fit.mean / math.sqrt(fit.sigma2 / fit.n)
    if abs(mean_t) > 2:
        warnings.append(
            f"estimation-window mean of R_i - R_m is {fit.mean:.6f} (t = {mean_t:.2f}): the market-adjusted "
            "model's alpha = 0, beta = 1 assumption looks violated"
        )
    return Inference(
        status=InferenceStatus.LIMITED if warnings else InferenceStatus.OK,
        null_hypothesis=f"E[CAR] = 0 over the {L}-session window, with AR = R_i - R_m",
        standard_error=se,
        standard_error_ignoring_estimation_error=se,
        test_statistic=t,
        distribution=f"Student t, {fit.df} df",
        degrees_of_freedom=fit.df,
        p_value=p,
        ci_low=lo,
        ci_high=hi,
        assumptions=[*COMMON_ASSUMPTIONS, "alpha = 0 and beta = 1 (no parameters are estimated)"],
        warnings=warnings,
    )


def placebo_distribution(residuals: np.ndarray, offsets: list[int], L: int, car: float) -> dict:
    """Empirical distribution of L-session sums of estimation-window abnormal returns.

    Only windows of L *consecutive* sessions with no missing observation are used, which keeps the
    serial-dependence structure of the data inside each placebo window. Placebo windows overlap,
    so they are not independent draws; the estimation-window residuals are in-sample (OLS forces
    them to sum to zero over the full sample), so the spread is slightly understated. Two-sided
    empirical p-value = (1 + #{|CAR_placebo| >= |CAR|}) / (1 + N).
    """
    by_off = dict(zip(offsets, residuals, strict=True))
    sums = []
    for start in offsets:
        block = [by_off.get(start + k) for k in range(L)]
        if all(v is not None for v in block):
            sums.append(float(np.sum(block)))
    out = {
        "method": "pre-event placebo windows (contiguous, in-sample estimation residuals)",
        "window_length": L,
        "placebo_windows": len(sums),
        "assumptions": [
            "event-window abnormal returns are exchangeable with pre-event abnormal returns under H0",
            "overlapping placebo windows are dependent; the p-value is approximate",
            "in-sample residuals slightly understate out-of-sample dispersion",
        ],
    }
    if len(sums) < MIN_PLACEBO_WINDOWS:
        out["status"] = "NOT_AVAILABLE"
        out["reason"] = f"only {len(sums)} complete placebo windows (< {MIN_PLACEBO_WINDOWS})"
        return out
    arr = np.asarray(sums)
    exceed = int(np.sum(np.abs(arr) >= abs(car) - 1e-15))
    out.update(
        status="OK",
        empirical_p_value=(1 + exceed) / (1 + len(sums)),
        placebo_quantiles={q: float(np.quantile(arr, q)) for q in (0.025, 0.05, 0.5, 0.95, 0.975)},
        exceedances=exceed,
    )
    return out
