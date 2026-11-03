# Multi-event study: QQQ vs SPY around MACRO_RELEASE events

> **SYNTHETIC DATA - METHODOLOGICAL VALIDATION ONLY. At least one input (offline equity prices) is synthetic. These estimates are NOT empirical findings about actual market responses and must not be used as investment evidence.**

Run `ES-52ff196a73b7e21d` - status **COMPLETED** - data origin `fixture:synthetic` - methodology `event-study-v0.3b.1`

## 1. Research question
How did QQQ respond, on average, around MACRO_RELEASE ['CPI', 'CORE_CPI'] events relative to SPY (market_model)?

## Sample
| item | value |
|---|---|
| category | MACRO_RELEASE |
| subtypes | ['CPI', 'CORE_CPI'] |
| target | QQQ |
| benchmark | SPY |
| model | market_model |
| price_basis | total_return |
| data_mode | research |
| as_of | n/a |
| start | n/a |
| end | n/a |
| first_release_only | True |
| max_events | 40 |
| events_selected | 30 |
| events_truncated_by_cap | 0 |
| selection_rule | all occurred events matching the filter, ordered by availability; no outcome-based selection |
| events_studied | 30 |
| events_eligible | 30 |
| events_excluded | 0 |
| events_skipped_time_budget | 0 |
| announcements | 15 |
| independent_clusters | 15 |
| overlapping_clusters | 0 |
| missing_data_rate | 0.0 |

## Aggregate (primary specification; unit = independent overlap cluster)
| window | clusters | mean raw | mean CAR | median CAR | sd CAR | test | p |
|---|---|---|---|---|---|---|---|
| [0,0] | 15 | +0.2190% | +0.1271% | -0.0356% | +1.1986% | LIMITED | 0.6875 |
| [0,+1] | 15 | +0.1999% | +0.0192% | -0.1208% | +1.7561% | LIMITED | 0.9668 |
| [-1,+1] | 15 | -0.2521% | -0.4644% | -1.1611% | +2.2916% | LIMITED | 0.4456 |
| [0,+5] | 15 | +0.7526% | +0.3897% | +0.0010% | +2.6814% | LIMITED | 0.5824 |

Significance is reported only as LIMITED and only with enough independent clusters; events released together count once, and overlapping windows are merged.

## Sensitivity (labelled; not the primary result)
| analysis | window | clusters | mean CAR |
|---|---|---|---|
| restricted_sample_excluding_confounded | [0,0] | 7 | -0.3354% |
| restricted_sample_excluding_confounded | [0,+1] | 7 | -1.1672% |
| restricted_sample_excluding_confounded | [-1,+1] | 7 | -1.5317% |
| restricted_sample_excluding_confounded | [0,+5] | 7 | -1.3015% |
| alternative model: market_adjusted | [0,0] | 15 | -0.1657% |
| alternative model: market_adjusted | [0,+1] | 15 | -0.2871% |
| alternative model: market_adjusted | [-1,+1] | 15 | -0.5709% |
| alternative model: market_adjusted | [0,+5] | 15 | +0.0450% |

## Events
| event | subtype | day 0 | status | cluster | confounders |
|---|---|---|---|---|---|
| EV-c41cbe683c8207c4 | CPI | 2025-07-15 | COMPLETED | C001 | 0 |
| EV-d881842d6db71253 | CORE_CPI | 2025-07-15 | COMPLETED | C001 | 0 |
| EV-647fd3e05bb82a41 | CORE_CPI | 2025-08-12 | COMPLETED | C002 | 0 |
| EV-7da9b06e5cb02670 | CPI | 2025-08-12 | COMPLETED | C002 | 0 |
| EV-1fd7c7098ea5b53e | CORE_CPI | 2025-09-11 | COMPLETED | C003 | 1 |
| EV-5c31f860c09c62a0 | CPI | 2025-09-11 | COMPLETED | C003 | 1 |
| EV-3662faec1f2208f3 | CORE_CPI | 2025-10-24 | COMPLETED | C004 | 1 |
| EV-96a9b269cd4bc708 | CPI | 2025-10-24 | COMPLETED | C004 | 1 |
| EV-123bb9c3fdd9bf74 | CORE_CPI | 2025-12-18 | COMPLETED | C005 | 1 |
| EV-2e05874417cb8a10 | CPI | 2025-12-18 | COMPLETED | C005 | 1 |
| EV-53cb3bda4333fd7a | CPI | 2026-01-13 | COMPLETED | C006 | 0 |
| EV-f8902ec032d1f1c5 | CORE_CPI | 2026-01-13 | COMPLETED | C006 | 0 |
| EV-7b459bc0480c5779 | CPI | 2026-02-13 | COMPLETED | C007 | 7 |
| EV-d09df55f2e52b2c3 | CORE_CPI | 2026-02-13 | COMPLETED | C007 | 7 |
| EV-64b8347c6c08ba64 | CORE_CPI | 2026-03-11 | COMPLETED | C008 | 10 |
| EV-94acdcaee7a9e67b | CPI | 2026-03-11 | COMPLETED | C008 | 10 |
| EV-abf207242205e4e6 | CORE_CPI | 2026-04-10 | COMPLETED | C009 | 9 |
| EV-cc680c41baf9822d | CPI | 2026-04-10 | COMPLETED | C009 | 9 |
| EV-858056dd849031ba | CPI | 2026-05-12 | COMPLETED | C010 | 0 |
| EV-bf115d70474e7333 | CORE_CPI | 2026-05-12 | COMPLETED | C010 | 0 |
| EV-109f3cf1bff7ecea | CPI | 2026-06-10 | COMPLETED | C011 | 1 |
| EV-3488d64152086896 | CORE_CPI | 2026-06-10 | COMPLETED | C011 | 1 |
| EV-091466c0169bd06d | CPI | 2026-07-14 | COMPLETED | C012 | 0 |
| EV-82f274ba10771b29 | CORE_CPI | 2026-07-14 | COMPLETED | C012 | 0 |
| EV-d37a96f38a08f8e3 | CPI | 2026-08-12 | COMPLETED | C013 | 0 |
| EV-d9573382af5e5fff | CORE_CPI | 2026-08-12 | COMPLETED | C013 | 0 |
| EV-bee5899eec1db670 | CPI | 2026-09-11 | COMPLETED | C014 | 1 |
| EV-c02d880421c0c3e4 | CORE_CPI | 2026-09-11 | COMPLETED | C014 | 1 |
| EV-cb03e87a0dd3b05f | CORE_CPI | 2026-10-14 | COMPLETED | C015 | 0 |
| EV-eeda2fff13dd7634 | CPI | 2026-10-14 | COMPLETED | C015 | 0 |

## 13. Limitations
- one target per comparison: several assets on the same announcement are never pooled as independent
- events released together count once; overlapping windows are merged into one cluster
- cross-sectional tests (LIMITED) assume independent clusters and ignore estimation-window overlap
- the primary specification was fixed before computing; restricted-sample and alternative-model results are sensitivity checks, not alternatives to choose from
- no clustered/panel inference for shared market shocks across assets is implemented

## 14. Reproducibility manifest
| item | value |
|---|---|
| run id | ES-52ff196a73b7e21d |
| specification hash | 17c1e0acd499aec659379e99d7b02f6140ca6cb3a82d66463f25e4947981e548 |
| data digest | d93bf6dbc473ea7e7e6f9364a447d510ab0db5b745a557b6222094c97529ccf7 |
| methodology | event-study-v0.3b.1 |
| software | {'finance_agent': '0.1.0', 'methodology': 'event-study-v0.3b.1', 'numpy': '2.5.4', 'scipy': '1.18.1', 'statsmodels': '0.15.0', 'exchange-calendars': '4.13.2'} |

Reproduce: `uv run python -m finance_agent study compare --event-type MACRO_RELEASE --ticker QQQ --benchmark SPY --model market_model --subtype CPI --subtype CORE_CPI`

