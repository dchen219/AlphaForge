# Treasury yield event study: DGS10 around RATE_CUT EV-e6a9f7badcb72f25

> **Inputs are recorded/live source data. Results are descriptive and statistical associations; they do not identify causal effects.**

Run `ES-4b16699726a5cc99` - status **COMPLETED** - data origin `fixture:real_snapshot` - methodology `event-study-v0.3b.1`

## 1. Research question
How did the DGS10 Treasury yield change around FOMC lowered the federal funds target range (2025-09-17)? (yield changes in basis points; no expected-change model)

## 5. Event time and trading-session alignment
Event time 2025-09-17T14:00:00-04:00; yield day 0 = **2025-09-17** - released before the ~15:30 ET quote time: day 0 = same observation day

## 9. Yield changes around the event (basis points; no abnormal-return model)
| rel | date | yield % | change | no-quote days bridged |
|---|---|---|---|---|
| -1 | 2025-09-16 | 4.04 | -1.0 bp |  |
| 0 | 2025-09-17 | 4.06 | +2.0 bp |  |
| 1 | 2025-09-18 | 4.11 | +5.0 bp |  |
| 2 | 2025-09-19 | 4.14 | +3.0 bp |  |
| 3 | 2025-09-22 | 4.15 | +1.0 bp |  |
| 4 | 2025-09-23 | 4.12 | -3.0 bp |  |
| 5 | 2025-09-24 | 4.16 | +4.0 bp |  |

| window | status | before (date) | after | change | share of pre-event abs. changes >= this |
|---|---|---|---|---|---|
| [0,0] | VALID | 4.04 (2025-09-16) | 4.06 | +2.0 bp | 0.777 |
| [0,+1] | VALID | 4.04 (2025-09-16) | 4.11 | +7.0 bp | 0.496 |
| [-1,+1] | VALID | 4.05 (2025-09-15) | 4.11 | +6.0 bp | 0.531 |
| [0,+5] | VALID | 4.04 (2025-09-16) | 4.16 | +12.0 bp | 0.312 |

Yield changes are in basis points of the yield level; they are not price returns (a Treasury ETF study measures returns in percent).

## 11. Confounding-event audit
Status **NONE_FOUND_WITHIN_COVERAGE**. Absence of confounders is verified only for the covered source families listed; it is not evidence that nothing else happened. Importance scores are heuristic rankings, not causal-impact measures.

Coverage:
| source family | status | stored range |
|---|---|---|
| macro (BLS/BEA via ALFRED) | COVERED | ['2025-07-03', '2026-10-30'] |
| central bank (Federal Reserve) | COVERED | ['2025-07-30', '2026-10-28'] |

Never covered: news, unscheduled announcements and analyst actions; disclosures by companies other than the target (and by benchmark constituents); macro releases AlphaForge does not ingest (e.g. PPI, retail sales, ISM, jobless claims); non-U.S. events and Treasury auctions

## 13. Limitations
- A_descriptive: observed price movement around the event (raw returns): description only
- B_benchmark_adjusted: not applicable: yield changes are reported without an expected-change model
- C_statistical_significance: not tested; the pre-event distribution of same-length changes is a descriptive reference only
- D_causal_identification: NOT ESTABLISHED. A significant abnormal return in the same window does not show that this event caused it (confounders, timing, model misspecification).
- daily end-of-day yields (~15:30 ET quotes): NOT an intraday reaction

## 14. Reproducibility manifest
| item | value |
|---|---|
| run id | ES-4b16699726a5cc99 |
| specification hash | ef9a40d2277b3b3e7f15ee73b53da5c27b1a5c9cb1b84c62e9081f3f472696b4 |
| data digest | 56ede9e5894b7866f2ae75e2d8a0fadb7f061cc3075079fff810dabdcb16f8f4 |
| methodology | event-study-v0.3b.1 |
| software | {'finance_agent': '0.1.0', 'methodology': 'event-study-v0.3b.1', 'numpy': '2.5.4', 'scipy': '1.18.1', 'statsmodels': '0.15.0', 'exchange-calendars': '4.13.2'} |

Reproduce: `uv run python -m finance_agent study run --event-id EV-e6a9f7badcb72f25 --series DGS10 --mode research`

