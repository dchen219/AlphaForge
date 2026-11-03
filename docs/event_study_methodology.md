# Event-study methodology (v0.3-B, `event-study-v0.3b.1`)

Implementation: `src/finance_agent/event_studies/`. Statistical validation:
[event_study_statistical_validation.md](event_study_statistical_validation.md). Data requirements:
[event_study_data_requirements.md](event_study_data_requirements.md). Market data and calendars:
[market_data_methodology.md](market_data_methodology.md),
[trading_calendar_methodology.md](trading_calendar_methodology.md).

All numbers come from deterministic code: statsmodels OLS, SciPy distributions and scalar
arithmetic. No LLM produces or interprets any estimate. Reports are template-based.

## 1. What a result can and cannot mean

Every result carries these four levels (`interpretation_scope`):

| Level | Produced? | Meaning |
|---|---|---|
| A. Descriptive movement | yes | observed (raw) returns or yield changes around the event |
| B. Benchmark-adjusted association | yes | abnormal returns relative to a stated benchmark model; an association, not an effect |
| C. Statistical significance | yes, with assumptions | whether the CAR is unusual relative to estimation-window noise |
| D. Causal identification | **no** | a significant abnormal return in the same window does not show the event caused it |

## 2. Data modes (different contracts)

| Mode | Prices and corporate actions | Status |
|---|---|---|
| `research` (Mode A, default) | the latest stored version of every bar, adjusted with all corporate actions known now | labelled `retrospectively_adjusted`. It is never described as what was known at the time. |
| `point_in_time` (Mode B, requires `as_of`) | only bars with `available_at <= as_of`, and only actions known by then (v0.3-A service) | see the rules below |

Point-in-time rules:
- An event not yet available at the cutoff makes the study `INELIGIBLE`.
- A window whose last daily bar is not final at the cutoff (D+1 00:00 New York) is
  `NOT_AVAILABLE_AT_CUTOFF`.
- If any bar used was first **retrieved after** the cutoff, its availability rests on the
  v0.3-A backfill assumption. The study is then `UNVERIFIED`, never `COMPLETED`.
- A study never switches silently to retrospective data.

The confounder audit also uses `as_of`. Treasury yields use only vintages available at the
cutoff.

## 3. Event time and session alignment (`daily-close-v1`)

The time used for alignment (`alignment.event_time_for`) is never invented:

| Event data | Time basis |
|---|---|
| `published_at` (EDGAR acceptance, Fed RSS pubDate) | `VERIFIED_TIMESTAMP` |
| `SCHEDULED_TIME_CONFIRMED_DATE` (BLS/BEA) | `OFFICIAL_SCHEDULE_CONFIRMED_DATE`: the official release time, on a date confirmed by ALFRED vintages |
| `DATE_ONLY_CONSERVATIVE` (`available_at` = D+1 00:00 ET) | `DATE_ONLY`: publication date D, time unknown |
| none of the above | `NONE`: ineligible |

Session boundaries are real UTC instants from the XNYS calendar, so DST and early closes are
handled by the calendar data.

| Class | Condition (America/New_York) | Day 0 |
|---|---|---|
| `PRE_MARKET` | before the regular open (including before 04:00) | same session |
| `REGULAR_SESSION` | open <= t < close | same session, **flagged**: the close-to-close return includes trading *before* the release |
| `AT_CLOSE` | t == close (16:00:00, or 13:00:00 on early-close days) | next session |
| `AFTER_HOURS` | t > close | next session |
| `NON_TRADING_DAY` | weekend or exchange holiday | next session |
| `UNCERTAIN_TIME` | date D only, D is a session | D. `[0,0]` is ineligible, and a window must contain days 0 and +1 because the release may have followed the close |
| `UNCERTAIN_TIME` on a non-session D | date D only, D not a session | next session (any time on D maps there) |
| `UNKNOWN` | no usable time or date | ineligible |

A daily close-to-close return is **never** described as an immediate post-announcement
response. The tests cover:
- 08:30 CPI (pre-market) and 14:00 FOMC (regular session, contamination flagged);
- 16:00:00 earnings (`AT_CLOSE`) and 16:05 earnings (`AFTER_HOURS`);
- the 2025-11-28 early close and the 2025-07-04 holiday;
- both 2026 DST transitions;
- date-only and unknown publication times.

## 4. Windows

Offsets are counted in trading sessions relative to day 0, never in calendar days.

| Window | Default |
|---|---|
| Estimation | sessions [-150, -21] (130 sessions); at least 100 valid **paired** observations |
| Event | [0,0], [0,+1], [-1,+1], [0,+5] |

All windows are configurable, with offsets bounded at ±500 sessions and at most 8 windows. The
estimation window must end before every event window starts.

Missing observations:
- A session is used for estimation only if both the asset and the benchmark have a valid return.
  Excluded sessions are listed with reasons (attrition).
- An event window with any missing observation is `MISSING_OBSERVATIONS`, with the sessions and
  reasons. It is **never shortened**, and returns are never filled or inferred.

## 5. Returns and price basis

Prices come only from the v0.3-A `MarketDataService`. The basis is explicit in every
specification: `raw`, `split_adjusted`, or `total_return` (the default).
- Provisional bars are excluded, and so are quarantined bars.
- Raw returns across a split ex-date are refused. A window containing one is
  `MISSING_OBSERVATIONS` ("split ex-date"); use `split_adjusted` or `total_return`.

| Quantity | Definition |
|---|---|
| simple return | R_t = P_t / P_{t-1} - 1 (consecutive sessions) |
| log return | r_t = ln(P_t / P_{t-1}) |
| compounded window return (descriptive) | Π(1 + R_t) - 1 (log: exp(Σ r_t) - 1) |
| abnormal return | AR_t = R_t - E[R_t] |
| **CAR(a,b)** | Σ_{t=a..b} AR_t: an arithmetic sum, **not** a compounded investment return |
| BHAR (descriptive) | compounded asset return - compounded benchmark return |

## 6. Expected-return models

| Model | Expected return | Abnormal return | Parameters |
|---|---|---|---|
| A `raw` | none | none; observed returns are reported | none (descriptive, no test) |
| B `market_adjusted` | R_m,t | AR_t = R_i,t - R_m,t (a simple difference) | none estimated. σ_d² = sample variance of R_i - R_m over the estimation window (n - 1 df). |
| C `market_model` | α̂ + β̂ R_m,t | AR_t = R_i,t - α̂ - β̂ R_m,t | OLS over the estimation window: R_i,t = α + β R_m,t + ε_t, σ̂² = e'e / (n - 2) |

Estimation is refused (`UnstableEstimation`) when:
- there are fewer than the minimum valid observations;
- benchmark variance is below 1e-12 (β is not identified);
- cond(X'X) exceeds 1e10.

A numerically exact fit (1 - R² < 1e-12) leaves no residual variance, so inference is
`NOT_AVAILABLE`. The report includes α, β, their standard errors, σ̂², df, R², the benchmark
variance, the condition number, and the expected returns and ARs for each event-window session.

## 7. Inference for a single event (market model)

With X = [1, R_m] over the estimation window (n rows), an event window of L sessions,
X* = [1, R_m*] and ι a vector of ones (Campbell, Lo & MacKinlay 1997, ch. 4; MacKinlay 1997):

```
Var(AR*)  = σ² (I_L + X* (X'X)⁻¹ X*')                       (L × L, not diagonal)
Var(CAR)  = ι' Var(AR*) ι = σ² ( L + g' (X'X)⁻¹ g ),   g = X*' ι = (L, Σ R_m*)
t         = CAR / sqrt(Var(CAR))  ~  Student t, n - 2 df     (H0: E[CAR] = 0)
95 % CI   = CAR ± t_{0.975, n-2} · SE
```

The second term of Var(CAR) is the estimation error in (α̂, β̂). It is shared by every
event-window AR, so the ARs are correlated and Var(CAR) is **not** simply L·σ². The report shows
both the SE and σ̂·√L ("SE w/o est. error") for comparison. The p-value is two-sided.

For the market-adjusted model, Var(CAR) = L·σ_d² with n - 1 df. The result is flagged when the
estimation-window mean of R_i - R_m is significantly non-zero (|t| > 2), because that contradicts
the model's α = 0, β = 1.

**Assumptions** (printed with every result):
- i.i.d. normal disturbances with constant variance;
- no event-induced volatility;
- parameter stability from the estimation window to the event window;
- no other information in the window.

**Placebo robustness check.** CARs over every contiguous L-session block of the
estimation-window residuals give an empirical two-sided p-value:
(1 + #{|CAR_placebo| >= |CAR|}) / (1 + N), reported only when N >= 30.
- Contiguity keeps the serial dependence inside each block.
- The blocks overlap, so the draws are dependent.
- The residuals are in-sample, so the spread is slightly understated.
- The placebo assumes event and pre-event ARs are exchangeable under H0, so it does **not** fix
  event-induced variance (see the validation doc).

**Inference is downgraded to `LIMITED`** when:
- the split-sample β is unstable;
- residuals are autocorrelated (windows longer than one session);
- event-induced volatility is elevated;
- residuals are non-normal (compare with the placebo p-value);
- the window contains a regular-session release on day 0;
- another recorded event falls in the window.

## 8. Diagnostics (`diagnostics.py`)

| Check | Statistic | Flag | Threatens |
|---|---|---|---|
| Outliers | standardized estimation residuals | \|z\| > 4 (listed; an exclusion variant is run) | OLS fit |
| Estimation gaps | missing sessions with reasons | any | sample size, representativeness |
| β stability | split-half β difference, z = Δβ / sqrt(se₁² + se₂²) | \|z\| > 2 | parameter stability |
| Autocorrelation | Ljung–Box (5 lags), Durbin–Watson | p < 0.05 | independence behind Var(CAR) |
| Normality | Jarque–Bera | p < 0.05 | exactness of the t reference |
| Event-induced volatility | mean(AR²) over event sessions / σ̂² | > 4 | test size (Boehmer, Musumeci & Poulsen 1991) |
| Low explanatory power | R² | < 0.05 | the usefulness of the benchmark |

## 9. Treasury yield studies (`yields_study.py`)

DGS2 and DGS10 are H.15 constant-maturity yields, in percent. They are **rates, not prices**, and
never pass through the equity return functions.

- **Observation days** are dates with a published value. ALFRED `.` rows mean there was no
  quote (federal or bond-market holidays). Changes that span a no-quote day list it; nothing is
  filled.
- **Day 0 (`yield-close-v1`).** The Treasury par yield curve uses indicative bid-side quotes
  collected by the Federal Reserve Bank of New York at or near 15:30 ET
  ([Treasury methodology](https://home.treasury.gov/policy-issues/financing-the-government/interest-rate-statistics/treasury-yield-curve-methodology)).
  - A release before 15:30 ET on an observation day falls in that day's value.
  - A release at or after 15:30, or on a no-quote day, falls in the next observation day.
  - Date-only releases follow the uncertainty rule in §3.
- **Changes:** ΔY_t = 100 × (Y_t - Y_{t-1}) bp. A window [a,b] change is 100 × (Y_b - Y_{a-1}) bp.
  A window beyond the last published observation is `OUT_OF_RANGE`, not shortened.
- **Reference:** the same-length changes over the estimation offsets (overlapping, dependent)
  give a mean, standard deviation and the share with a larger absolute change. This is
  **descriptive** context, not a significance test, because there is no expected-change model
  for yields.
- Treasury ETF studies (SHY/IEF/TLT) measure price **returns in percent**; yield studies measure
  **basis points**. Daily yields cannot show an intraday reaction.

## 10. Confounding-event audit (`confounders.py`)

Every other occurred event in the store is aligned with the same rules. It is listed when its
day 0 falls inside the union of the event windows.

| Event | Relevant to |
|---|---|
| `MACRO_RELEASE`, `CENTRAL_BANK` | every study (market-wide) |
| corporate filings, earnings, other disclosures | studies of the company it names only |
| events released together with the study event (same source, category, instant/date), and revisions published with it | `same_announcement`, not a confounder |
| `FINANCIAL_METRIC_CHANGE` (derived from a filing) | `derived`, not independent information |

Each entry reports the event id, type, time, entities, relative session, importance score
(labelled heuristic, **not** causal impact), source, and the reason.

**Coverage.** No ingestion-window record exists for events, so coverage per source family
(BLS/BEA, Fed, SEC filings for the target) is **inferred** from the range of stored events.
- The status is `CONFOUNDERS_PRESENT`, `NONE_FOUND_WITHIN_COVERAGE` or `COVERAGE_INCOMPLETE`.
  An absence of confounders is reported as "none found within coverage", never as verified
  absence.
- Sources never covered are listed: news, other companies, macro releases that are not
  ingested, non-U.S. events.

Candidates are selected by availability within ±14 days of the window.

Multi-event comparisons report the full sample with confounder flags, a **restricted** sample that
drops clusters with confounders of predefined types (default: macro, central bank, earnings), and
the difference between the two.

## 11. Multi-event analysis (`aggregation.py`)

Selection is fixed before computing: all occurred events matching the filter, first releases only
by default, one target, at most 100 events, and a wall-clock budget (a run that hits it is
`PARTIAL`).

1. **Announcements.** Events released together count once, e.g. CPI with core CPI, or payrolls
   with the unemployment rate. For one asset they have identical windows (avoiding
   pseudo-replication).
2. **Overlap clusters.** Announcements whose event-window spans share a session are merged
   (union–find). The cluster mean is one observation.
3. **Statistics over clusters**, per window:
   - mean raw return, mean CAR, mean AR per session, median, SD, min/max, share positive, BHAR;
   - counts of eligible, excluded and skipped events;
   - the missing-data rate.
4. **Significance.** A cross-sectional t-test of the mean cluster CAR (Brown & Warner 1985 style)
   is reported only with **at least 10 independent clusters**, and always as `LIMITED`. It assumes
   independent clusters and ignores the correlation from overlapping estimation windows.
   Otherwise the result is descriptive only.
5. **No pooling of assets.** Several assets responding to the same macro announcement are never
   pooled as independent observations. Clustered or panel inference for shared shocks across
   assets (e.g. Kolari & Pynnönen 2010) is **not implemented**.

## 12. Robustness (single event)

These specifications are predefined, run after the primary, and **all** reported, including
failures:
- the other expected-return models;
- alternative benchmarks (SPY / QQQ / sector ETF, never chosen by outcome);
- estimation windows [-250,-21] (minimum 150), [-120,-11] (minimum 80), and a small sample
  [-60,-11] (minimum 40);
- log returns;
- exclusion of |z| > 4 estimation outliers.

Robustness checks are not evidence of causality, and windows are never optimized on the
evaluation sample.

## 13. Reproducibility and persistence

Every analysis is an immutable `EventStudySpecification`. It records:
- the event id, source, the time used and its basis, and the category filter;
- the target and benchmark;
- the basis, return type, market-data source, alignment convention and data mode / `as_of`;
- the windows, the model and the test method;
- quality exclusions, excluded estimation sessions and the methodology version.

The run id is built as follows:
- `spec_hash` = SHA-256 of the canonical specification JSON.
- `data_digest` = SHA-256 of the exact data used: bar digests (session, version, close, factor,
  snapshot), adjustment factors, the event's immutable facts, confounder ids, and the robustness
  request.
- `run_id = "ES-" + sha256(kind, spec_hash, data_digest, methodology)[:16]`.

Consequences:
- Identical inputs return the **stored** run; it is not recomputed or overwritten.
- Changed prices (e.g. a provider correction), events, assumptions or code version give a
  **new** run, and older runs remain retrievable.
- Each run stores its specification, the data manifest (bar versions, snapshot SHA-256s,
  corrected sessions, factors, contract statement), the result, the report, software versions
  (numpy/scipy/statsmodels/exchange-calendars) and a reproduction command.
- Exact offline replay holds while the referenced snapshots and bar versions exist.

Tables: `event_study_runs`, `event_study_results` (migration `0006_event_studies`).

## 14. Reports and data-origin labelling

Reports come in JSON sections and Markdown with 14 sections, from the research question to the
reproducibility manifest. If **any** input is synthetic, the run is `fixture:synthetic` with
`synthetic: true`, and the label appears in four places:
- the report banner: "SYNTHETIC DATA - METHODOLOGICAL VALIDATION ONLY ... NOT empirical findings";
- the API (`notice`);
- the CLI (stderr warning);
- the log record.

Mixed real inputs (e.g. recorded events with live yields) are labelled `mixed:...`. An optional
LLM narrative is not part of v0.3-B; template reports work without any API key.

## 15. Known limitations

- **Equity data:** live equity prices (Alpaca) are unverified, and every offline equity result
  is methodological validation on synthetic prices.
- **Inference scope:**
  - single-event inference is classical, assuming normal, i.i.d. errors;
  - no BMP standardized cross-sectional test, Kolari–Pynnönen adjustment or rank tests;
  - no intraday data.
- **Event data:**
  - confounder coverage is inferred, and AlphaForge ingests a limited set of sources;
  - there is no news, analyst or consensus source.
- **Point-in-time studies over backfilled bars are `UNVERIFIED`.**

## References

- MacKinlay, A. C. (1997). Event Studies in Economics and Finance. *Journal of Economic
  Literature*, 35(1), 13–39.
- Campbell, J. Y., Lo, A. W., & MacKinlay, A. C. (1997). *The Econometrics of Financial
  Markets*, ch. 4 (Event-Study Analysis). Princeton University Press.
- Brown, S. J., & Warner, J. B. (1985). Using daily stock returns: The case of event studies.
  *Journal of Financial Economics*, 14(1), 3–31.
- Boehmer, E., Musumeci, J., & Poulsen, A. B. (1991). Event-study methodology under conditions of
  event-induced variance. *Journal of Financial Economics*, 30(2), 253–272.
- Kolari, J. W., & Pynnönen, S. (2010). Event study testing with cross-sectional correlation of
  abnormal returns. *Review of Financial Studies*, 23(11), 3996–4025.
- Ljung, G. M., & Box, G. E. P. (1978). On a measure of lack of fit in time series models.
  *Biometrika*, 65(2), 297–303.
- Jarque, C. M., & Bera, A. K. (1987). A test for normality of observations and regression
  residuals. *International Statistical Review*, 55(2), 163–172.
- U.S. Department of the Treasury, Treasury Yield Curve Methodology (quotes at or near 3:30 PM ET).
