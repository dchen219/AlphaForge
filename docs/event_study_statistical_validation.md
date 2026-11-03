# Event-study statistical validation (v0.3-B)

What has been checked, against what **independent** reference, and what remains unvalidated.
None of these checks treats a match against the implementation itself as validation. All data
below is SYNTHETIC (constructed in tests or by the seeded fixture generator), except the Treasury
yield checks, which use REAL ALFRED vintages.

## 1. Closed-form and independent-implementation checks (`tests/unit/test_event_study_core.py`)

| Check | Independent reference | Tolerance |
|---|---|---|
| OLS α, β, σ², SEs | `numpy.linalg.lstsq` and an explicit σ²(X'X)⁻¹ (the implementation uses statsmodels) | 1e-10 to 1e-12 |
| Var(CAR) | explicit L×L matrix ι'σ²(I + X*(X'X)⁻¹X*')ι (the implementation uses the g-vector form) | 1e-12 relative |
| Per-session Var(AR) | diagonal of the same matrix | 1e-12 relative |
| Hand-derived example | see §2 | 1e-12 |
| Cross-sectional t-test | `scipy.stats.ttest_1samp` | 1e-10 |
| Market-adjusted model | direct difference and `numpy.var(ddof=1)` | exact |

## 2. Hand-derived example

The inputs are chosen so that OLS is exact:
- R_m = (-0.02, -0.01, 0, 0.01, 0.02);
- residuals e = (0.001, -0.002, 0, 0.002, -0.001), orthogonal to (1, R_m);
- R_i = 0.001 + 1.5 R_m + e.

Then α̂ = 0.001 and β̂ = 1.5 exactly, SSR = 1e-5, σ̂² = 1e-5/3 and X'X = diag(5, 0.001).

For an event window with R_m* = (0.01, 0.03) and an injected +1 % on day 0:
- g = (2, 0.04) and g'(X'X)⁻¹g = 4/5 + 0.0016/0.001 = 2.4;
- Var(CAR) = σ̂²(2 + 2.4) = 1.4667e-5, so SE = 0.0038297;
- t = 0.01 / 0.0038297 = 2.611 with 3 df;
- σ̂·√L = 0.0025820. Ignoring estimation error would understate the SE by a third.

## 3. Known-effect recovery

- An exact model (no noise) gives AR = 0 to 1e-12, and inference is `NOT_AVAILABLE` (σ² = 0).
  A regression test covers the earlier bug where floating-point σ² ≈ 1e-35 produced a
  t-statistic.
- **Injected effects:** +5 % and -4 % on day 0 are recovered within 0.01, with p < 1e-6 and the
  true effect inside the CI.
- **Integration test against the SYNTHETIC fixture's injected move** (XLE +30.66 % on
  2025-08-14):
  - α, β and CARs [0,0], [-1,+1] and [0,+5] match numpy `lstsq` computed straight from the raw
    fixture JSON, to 1e-12;
  - the compounded window return matches Π(1+R)-1.

## 4. Monte Carlo size and coverage (seeded)

Setup: n = 130 estimation sessions, R_m ~ N(0, 0.01²), R_i = 0.0002 + 0.9 R_m + N(0, 0.015²),
1,500 replications per row, seed 20261102. The binomial standard error at 5 % is about 0.0056.

| Event window | Size at 5 % | 95 % CI coverage | Size with the naive SE σ̂√L |
|---|---|---|---|
| L = 1 | 0.0460 | 0.9540 | 0.0480 |
| L = 3 | 0.0513 | 0.9487 | 0.0560 |
| L = 6 | 0.0487 | 0.9513 | 0.0527 |

The implemented test has the nominal size. At n = 130 the estimation-error term is small, so the
naive SE over-rejects only mildly; it matters more for short estimation windows (see §2). The test
suite asserts size and coverage within 4 binomial standard errors at L = 3.

**When assumptions fail** (L = 3, seed 7, 1,500 replications):

| Data-generating process | Classical size | Placebo size |
|---|---|---|
| normal, no event-induced variance | 0.0467 | 0.0513 |
| event-window SD × 2 | **0.3193** | **0.3307** |
| Student-t(4) errors | 0.0553 | 0.0580 |

Event-induced variance makes both tests over-reject badly. The placebo check does not protect
against it, because it assumes event and pre-event ARs are exchangeable. That is why
`event_induced_volatility` is a diagnostic that downgrades inference to `LIMITED`. Heavy tails
cost little at this sample size.

## 5. Alignment, windows and data integrity

| Area | Coverage |
|---|---|
| Alignment and windows | unit tests for every timing class, the early close, holiday, both DST transitions, date-only and unknown times, and session-grid offsets around holidays |
| Integration | missing benchmark session (IWM 2025-03-14 → windows invalid, never shortened); raw-basis refusal across the NVDA split; provisional bar on 2026-10-30; after-hours earnings |
| Treasury yields | changes reconcile to 100 × ΔY computed directly from the recorded ALFRED CSV (REAL); the live ALFRED vintage reproduces the same values (§6) |
| Historical replay (`tests/historical_replay/test_event_study_replay.py`) | event unknown at cutoff; windows not final at cutoff; backfilled bars → `UNVERIFIED`; price correction after the cutoff invisible to earlier cutoffs (old run preserved); future split absent from point-in-time factors; a confounder published in the window but available after the cutoff hidden point-in-time |

## 6. Live validation (2026-11-02)

**Treasury yields: live data verified.**
- A live ALFRED vintage (2026-11-02, keyless) was ingested and run through the yield study for
  REAL FOMC decisions.
- DGS10 around the 2025-09-17 cut: +2 bp [0,0], +7 bp [0,+1], +6 bp [-1,+1], +12 bp [0,+5].
  These are identical to the recorded-fixture result.
- `tests/integration/test_live.py::test_live_v03b_yield_event_study_on_live_alfred` (opt-in)
  passed.

**Equity prices: not validated.** No Alpaca credentials are available. Every equity CAR in this
repository is computed on SYNTHETIC prices and demonstrates the method only.

## 7. Not validated / not implemented

- Real equity-price event studies (pending live Alpaca credentials).
- Standardized cross-sectional (BMP), Kolari–Pynnönen and rank/sign tests.
- Clustered or panel inference across assets.
- Intraday windows.
- The placebo p-value's finite-sample behaviour beyond the experiments in §4.
- Coverage of the confounder audit beyond the ingested sources.
