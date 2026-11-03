"""Expected-return models (MacKinlay 1997; Campbell, Lo & MacKinlay 1997, ch. 4).

* Model A ``raw``: no expected return; the observed return is reported (descriptive).
* Model B ``market_adjusted``: AR_t = R_it - R_mt (a simple difference; nothing is estimated
  for the expected return; the estimation window only supplies Var(AR) for inference).
* Model C ``market_model``: OLS over the estimation window
  R_it = alpha + beta R_mt + e_t, Var(e) = sigma^2, then AR_t = R_it - (alpha_hat + beta_hat R_mt).

OLS is statsmodels ``OLS`` (a maintained, validated implementation); tests check it against an
independent closed-form computation. Only targeted statsmodels submodules are imported.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CONDITION_LIMIT = 1e10
MIN_BENCHMARK_VARIANCE = 1e-12


class UnstableEstimation(ValueError):
    """Too few observations, no benchmark variation, or a (near-)singular design matrix."""


@dataclass(frozen=True)
class MarketModelFit:
    n: int
    alpha: float
    beta: float
    alpha_se: float
    beta_se: float
    sigma2: float  # residual variance, e'e / (n - 2)
    df: int  # n - 2
    r_squared: float
    xtx_inv: np.ndarray  # (X'X)^-1, 2x2
    residuals: np.ndarray
    benchmark_variance: float
    condition_number: float

    def expected(self, rm: np.ndarray) -> np.ndarray:
        return self.alpha + self.beta * rm

    def abnormal(self, ri: np.ndarray, rm: np.ndarray) -> np.ndarray:
        return ri - self.expected(rm)

    def ar_variance(self, rm: np.ndarray) -> np.ndarray:
        """Var(AR_t) = sigma^2 (1 + x_t' (X'X)^-1 x_t), x_t = (1, R_mt)."""
        x = np.column_stack([np.ones_like(rm), rm])
        return self.sigma2 * (1.0 + np.einsum("ij,jk,ik->i", x, self.xtx_inv, x))


@dataclass(frozen=True)
class MarketAdjustedFit:
    n: int
    mean: float  # estimation-window mean of R_i - R_m (should be ~0 under the model)
    sigma2: float  # sample variance of R_i - R_m (ddof = 1)
    df: int  # n - 1
    residuals: np.ndarray  # R_i - R_m over the estimation window


def fit_market_model(ri: np.ndarray, rm: np.ndarray, min_obs: int) -> MarketModelFit:
    from statsmodels.regression.linear_model import OLS

    ri, rm = np.asarray(ri, float), np.asarray(rm, float)
    n = len(ri)
    if n != len(rm):
        raise ValueError("asset and benchmark samples must be paired")
    if n < max(min_obs, 3):
        raise UnstableEstimation(f"only {n} paired estimation observations (< {max(min_obs, 3)})")
    var_m = float(np.var(rm, ddof=1))
    if not np.isfinite(var_m) or var_m < MIN_BENCHMARK_VARIANCE:
        raise UnstableEstimation("benchmark returns have (almost) no variation: beta is not identified")
    x = np.column_stack([np.ones(n), rm])
    xtx = x.T @ x
    cond = float(np.linalg.cond(xtx))
    if not np.isfinite(cond) or cond > CONDITION_LIMIT:
        raise UnstableEstimation(f"design matrix is (near-)singular (condition number {cond:.3g})")
    res = OLS(ri, x).fit()
    alpha, beta = (float(v) for v in res.params)
    sigma2 = float(res.scale)
    se = np.sqrt(np.diag(res.cov_params()))
    return MarketModelFit(
        n=n, alpha=alpha, beta=beta, alpha_se=float(se[0]), beta_se=float(se[1]), sigma2=sigma2,
        df=int(res.df_resid), r_squared=float(res.rsquared), xtx_inv=np.linalg.inv(xtx),
        residuals=np.asarray(res.resid, float), benchmark_variance=var_m, condition_number=cond,
    )  # fmt: skip


def fit_market_adjusted(ri: np.ndarray, rm: np.ndarray, min_obs: int) -> MarketAdjustedFit:
    d = np.asarray(ri, float) - np.asarray(rm, float)
    n = len(d)
    if n < max(min_obs, 3):
        raise UnstableEstimation(f"only {n} paired estimation observations (< {max(min_obs, 3)})")
    return MarketAdjustedFit(n=n, mean=float(d.mean()), sigma2=float(d.var(ddof=1)), df=n - 1, residuals=d)
