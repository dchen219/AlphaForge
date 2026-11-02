# Event study: QQQ around RATE_CUT EV-e6a9f7badcb72f25

> **SYNTHETIC DATA - METHODOLOGICAL VALIDATION ONLY. At least one input (offline equity prices) is synthetic. These estimates are NOT empirical findings about actual market responses and must not be used as investment evidence.**

Run `ES-4099bab9758a2274` - status **COMPLETED** - data origin `fixture:synthetic` - methodology `event-study-v0.3b.1`

## 1. Research question
How did QQQ perform around FOMC lowered the federal funds target range (2025-09-17) (RATE_CUT), and was the move unusual relative to the market_model benchmark model on SPY?

## 2. Event definition and source
| field | value |
|---|---|
| event_id | EV-e6a9f7badcb72f25 |
| title | FOMC lowered the federal funds target range (2025-09-17) |
| event_type | CENTRAL_BANK |
| event_subtype | RATE_CUT |
| source_name | Federal Reserve |
| source_url | https://www.federalreserve.gov/newsevents/pressreleases/monetary20250917a.htm |
| source_document_id | monetary20250917a.htm |
| published_at | 2025-09-17T18:00:00+00:00 |
| available_at | 2025-09-17T18:00:00+00:00 |
| availability_precision | EXACT_TIMESTAMP |
| data_origin | fixture:real_snapshot |

## 3. Asset and benchmark
Target `QQQ`; benchmark `SPY` - S&P 500 ETF (broad U.S. large-cap market proxy)

## 4. Data origin and adjustment basis
| field | value |
|---|---|
| data_origin | fixture:synthetic |
| synthetic | True |
| price_basis | total_return |
| return_type | simple |
| data_mode | research |
| as_of | n/a |
| contract_statement | RETROSPECTIVE research view: latest data adjusted with all corporate actions known now; prices are not necessarily what an investor saw on those dates. |
| retrospectively_adjusted | True |
| adjustment_method | adjustment-v0.3a.1 |
| quality_exclusions | ['PROVISIONAL', 'QUARANTINED'] |

## 5. Event time and trading-session alignment
Timing class **REGULAR_SESSION** (VERIFIED_TIMESTAMP); event time 2025-09-17T14:00:00-04:00; day 0 = **2025-09-17**. during the regular session: day 0 = same session

**Warning:** released during regular trading: the day-0 close-to-close return includes trading BEFORE the release (pre-announcement contamination).

## 6. Estimation and event-window definitions
Estimation offsets [-150, -21] (minimum 100 valid paired observations; 130 valid, 0 missing - never filled). Event windows in trading sessions:

| window | first session | last session | status |
|---|---|---|---|
| [0,0] | 2025-09-17 | 2025-09-17 | VALID |
| [0,+1] | 2025-09-17 | 2025-09-18 | VALID |
| [-1,+1] | 2025-09-16 | 2025-09-18 | VALID |
| [0,+5] | 2025-09-17 | 2025-09-24 | VALID |

## 7. Expected-return model
Model **market_model**.
| alpha | se(alpha) | beta | se(beta) | sigma^2 | df | R^2 |
|---|---|---|---|---|---|---|
| 3.61598e-05 | 0.001163 | 0.1630 | 0.1293 | 0.000175853 | 128 | 0.0123 |

## 8. Raw returns
| window | status | asset cumulative | benchmark cumulative | BHAR |
|---|---|---|---|---|
| [0,0] | VALID | -0.9027% | +1.0863% | -1.9890% |
| [0,+1] | VALID | -2.3518% | +0.3453% | -2.6971% |
| [-1,+1] | VALID | -2.2589% | -0.1360% | -2.1229% |
| [0,+5] | VALID | -1.4923% | +3.6155% | -5.1078% |

## 9. Abnormal returns and CAR
| rel | session | R_i | R_m | E[R_i] | AR | note |
|---|---|---|---|---|---|---|
| -1 | 2025-09-16 | +0.0951% | -0.4797% | -0.0746% | +0.1697% |  |
| 0 | 2025-09-17 | -0.9027% | +1.0863% | +0.1807% | -1.0834% |  |
| 1 | 2025-09-18 | -1.4623% | -0.7330% | -0.1159% | -1.3464% |  |
| 2 | 2025-09-19 | -0.1338% | +1.3764% | +0.2280% | -0.3618% |  |
| 3 | 2025-09-22 | +2.8265% | -0.0340% | -0.0019% | +2.8284% |  |
| 4 | 2025-09-23 | -0.1785% | +0.9494% | +0.1584% | -0.3369% |  |
| 5 | 2025-09-24 | -1.5856% | +0.9334% | +0.1558% | -1.7414% |  |

| window | CAR | status / reason |
|---|---|---|
| [0,0] | -1.0834% | VALID |
| [0,+1] | -2.4298% | VALID |
| [-1,+1] | -2.2602% | VALID |
| [0,+5] | -2.0415% | VALID |

CAR is the arithmetic sum of abnormal returns, not a compounded investment return.

## 10. Statistical inference
| window | status | SE(CAR) | SE w/o est. error | t | df | p | 95% CI | placebo p |
|---|---|---|---|---|---|---|---|---|
| [0,0] | LIMITED | 0.013384 | 0.013261 | -0.809 | 128 | 0.4197 | [-3.7316%, +1.5648%] | 0.4122 |
| [0,+1] | LIMITED | 0.018902 | 0.018754 | -1.285 | 128 | 0.2009 | [-6.1699%, +1.3103%] | 0.2462 |
| [-1,+1] | LIMITED | 0.023233 | 0.022969 | -0.973 | 128 | 0.3325 | [-6.8572%, +2.3369%] | 0.3488 |
| [0,+5] | LIMITED | 0.033528 | 0.032483 | -0.609 | 128 | 0.5437 | [-8.6755%, +4.5926%] | 0.6190 |

Assumptions:
- estimation-window disturbances are i.i.d. normal with constant variance
- the event does not change the variance of returns (no event-induced volatility)
- the expected-return model and its parameters are stable from the estimation window to the event window
- no other information affecting the asset is released in the event window (see confounder audit)
- statistical significance is evidence of an unusual return, NOT causal identification of the event
- Var(CAR) includes alpha/beta estimation error and the covariance it induces between event-window ARs

- [0,0]: day-0 return includes pre-release trading
- [0,+1]: day-0 return includes pre-release trading
- [-1,+1]: day-0 return includes pre-release trading
- [0,+5]: day-0 return includes pre-release trading

## 11. Confounding-event audit
Status **COVERAGE_INCOMPLETE**. Absence of confounders is verified only for the covered source families listed; it is not evidence that nothing else happened. Importance scores are heuristic rankings, not causal-impact measures.

Coverage:
| source family | status | stored range |
|---|---|---|
| macro (BLS/BEA via ALFRED) | COVERED | ['2025-07-03', '2026-10-30'] |
| central bank (Federal Reserve) | COVERED | ['2025-07-30', '2026-10-28'] |
| SEC EDGAR filings for QQQ | NOT_COVERED |  |

Never covered: news, unscheduled announcements and analyst actions; disclosures by companies other than the target (and by benchmark constituents); macro releases AlphaForge does not ingest (e.g. PPI, retail sales, ISM, jobless claims); non-U.S. events and Treasury auctions

## 12. Robustness results
All attempted specifications are listed, including failures. The primary specification was fixed before these were run.

| variant | status | CAR [0,0] | CAR [0,+1] | CAR [-1,+1] | CAR [0,+5] | p [0,0] | p [0,+1] | p [-1,+1] | p [0,+5] |
|---|---|---|---|---|---|---|---|---|---|
| model: raw | COMPLETED | -0.9027% | -2.3518% | -2.2589% | -1.4923% | n/a | n/a | n/a | n/a |
| model: market_adjusted | COMPLETED | -1.9890% | -2.7183% | -2.1435% | -5.0149% | 0.194 | 0.209 | 0.418 | 0.181 |
| estimation [-250,-21] min 150 | COMPLETED | -1.0406% | -2.4086% | -2.2515% | -1.8886% | 0.421 | 0.188 | 0.316 | 0.556 |
| estimation [-120,-11] min 80 | COMPLETED | -1.0937% | -2.5334% | -2.4455% | -2.2372% | 0.412 | 0.179 | 0.291 | 0.504 |
| small sample [-60,-11] min 40 | COMPLETED | -0.9542% | -2.5094% | -2.5014% | -1.8123% | 0.454 | 0.167 | 0.263 | 0.583 |
| log returns | COMPLETED | -1.0794% | -2.4274% | -2.2491% | -2.0585% | 0.421 | 0.201 | 0.335 | 0.540 |

## 13. Limitations
- A_descriptive: observed price movement around the event (raw returns): description only
- B_benchmark_adjusted: abnormal returns relative to the stated benchmark model: an association, not an effect
- C_statistical_significance: a classical test of whether the CAR is unusual relative to the estimation-window noise, under the stated assumptions
- D_causal_identification: NOT ESTABLISHED. A significant abnormal return in the same window does not show that this event caused it (confounders, timing, model misspecification).
- released during regular trading: the day-0 close-to-close return includes trading BEFORE the release (pre-announcement contamination)
- daily close-to-close returns: NOT an immediate post-announcement (intraday) response
- low market-model R-squared (0.012): the benchmark explains little variation

## 14. Reproducibility manifest
| item | value |
|---|---|
| run id | ES-4099bab9758a2274 |
| specification hash | fd29114526b4d8c99a9ce989459204a9cd4533d4a12ba9371f6f9a4989d71496 |
| data digest | 3e791d917a10b3ef00cbce17ebdade1ae5ca60a519292831d2192567921e3ae2 |
| methodology | event-study-v0.3b.1 |
| software | {'finance_agent': '0.1.0', 'methodology': 'event-study-v0.3b.1', 'numpy': '2.5.4', 'scipy': '1.18.1', 'statsmodels': '0.15.0', 'exchange-calendars': '4.13.2'} |
- target `QQQ`: 157 bars, versions [1], 1 snapshot(s), corrected sessions 0, adjustment factors 11, bars digest `a5312edde58c2265`
- benchmark `SPY`: 157 bars, versions [1], 1 snapshot(s), corrected sessions 0, adjustment factors 11, bars digest `eec09ae2fdb4b2eb`

Reproduce: `uv run python -m finance_agent study run --event-id EV-e6a9f7badcb72f25 --ticker QQQ --benchmark SPY --model market_model --basis total_return --mode research`

