# Event Intelligence Methodology (AlphaForge v0.2)

How official source data becomes ranked, explained, point-in-time-correct financial events.
Code is the source of truth; module paths are given for each rule.

## 1. Scope and boundary

v0.2 detects, normalizes, deduplicates, scores and explains **verified** events. It does
**not** estimate causal effects, predict prices, or report market reactions or
consensus surprises (no authenticated consensus or market-price source is integrated).
Statistical event studies and impact prediction are v0.3 scope.

## 2. Event taxonomy (`events/schemas.py`)

Controlled enums, validated per category (`SUBTYPES_BY_CATEGORY`).

| Category | Subtypes (v0.2) | Source |
|---|---|---|
| MACRO_RELEASE | CPI, CORE_CPI, PCE_PRICE_INDEX, CORE_PCE_PRICE_INDEX, NONFARM_PAYROLLS, UNEMPLOYMENT_RATE, REAL_GDP, RELEASE_SCHEDULED | BLS/BEA calendars + ALFRED vintages |
| CENTRAL_BANK | RATE_HIKE, RATE_CUT, RATE_HOLD, POLICY_STATEMENT, MEETING_SCHEDULED | Federal Reserve calendar, RSS, statements |
| CORPORATE_FILING | FORM_10K, FORM_10Q, FORM_8K, AMENDMENT, FINANCIAL_METRIC_CHANGE | SEC EDGAR submissions + XBRL (v0.1 engine) |
| CORPORATE_EARNINGS | EARNINGS_RELEASE (8-K item 2.02) | SEC submissions `items` |
| OTHER_DISCLOSURE | LEADERSHIP_CHANGE (5.02), MATERIAL_AGREEMENT (1.01/1.02), FINANCING (2.03/2.04/3.02), ACQUISITION_DISPOSITION (2.01), CYBERSECURITY_INCIDENT (1.05), REG_FD_DISCLOSURE (7.01), SHAREHOLDER_VOTE (5.07), GOVERNANCE_CHANGE (3.03/5.03), OTHER_MATERIAL_EVENT | SEC submissions `items` |
| CORPORATE_GUIDANCE | GUIDANCE_UPDATE (reserved) | none in v0.2 |

**Status** is separate from type: `SCHEDULED` (calendar only), `PUBLISHED`, `REVISED`,
`CANCELLED`, `POSTPONED`. A scheduled entry carries no observed values (enforced by the
schema) and is **never** evidence that a release occurred. `CANCELLED`/`POSTPONED` are
modelled but only assigned from an explicit source statement; a passed scheduled date
without confirming data is marked `UNCONFIRMED`, not cancelled.

8-K classification uses only the SEC's own item metadata, in priority order
(2.02 > 4.02 > 1.05 > 2.01 > 1.01 > …). An 8-K with only item 9.01 (exhibits) remains a
generic `FORM_8K`. Not every 8-K is treated as earnings.

## 3. Sources and reliability

| Source | Access | Reliability role |
|---|---|---|
| SEC EDGAR submissions | data.sec.gov JSON, declared User-Agent, ≤5 req/s | primary, official |
| Federal Reserve | fomccalendars.htm, press_monetary.xml, statement pages; no robots.txt restrictions | primary, official |
| BLS calendar | bls.gov/schedule/news_release/bls.ics (robots-permitted) | schedule only |
| BEA calendar | bea.gov/news/schedule (robots-permitted; upcoming releases only) | schedule only |
| ALFRED | alfred.stlouisfed.org/graph/alfredgraph.csv (keyless; crawl-delay 2 s honoured) | primary values + vintage confirmation |

No news sites, paywalled or licensed data are used. FRED's keyed API (v0.1 provider) is
not required for v0.2.

## 4. Publication and availability timing

Every event has `available_at` (the earliest instant it may be used in replay) and an
`availability_precision`:

| Precision | Used for | Rule |
|---|---|---|
| EXACT_TIMESTAMP | SEC filings | EDGAR `acceptanceDateTime` (verified to be true UTC against the SGML header `ACCEPTANCE-DATETIME` in ET, for EDT and EST filings) |
| EXACT_TIMESTAMP | FOMC statements | RSS `pubDate`, else the statement's own "For release at H:MM p.m. EDT/EST" |
| SCHEDULED_TIME_CONFIRMED_DATE | BLS/BEA releases | Official scheduled time (e.g. 08:30 ET) **and** the publication *date* confirmed by ALFRED (observation absent in vintage D−1, present in vintage D). The intraday time itself is not independently verified. Disable with `MACRO_USE_CONFIRMED_SCHEDULED_TIME=false`. |
| DATE_ONLY_CONSERVATIVE | releases without a scheduled time; metric-change events | v0.1 policy: available from D+1 00:00 America/New_York |
| UNKNOWN | calendar entries | never used as occurred events |

The observation period is never used as a publication time. Date-only CLI/API inputs mean
23:59:59 New York (v0.1 convention). All timestamps are timezone-aware and stored in UTC.

Limitations: calendars are fetched as of today (no calendar vintages), so a historical run
sees today's version of a schedule; this affects only `SCHEDULED` entries, never
occurred events. EDGAR dissemination lag after acceptance is not modelled.

## 5. Macro release detection

1. Calendar → scheduled entries (`SCHED|<group>|<date>`).
2. Confirmation: for the group's primary series, compare ALFRED vintages D−1 and D. A
   new observation (or changed values) confirms publication on D.
3. BEA history (not on the public schedule): month-end probes, then bisection over
   vintages to find the first date the change appears.
4. For every series in the release group: new observations → `PUBLISHED` first-release
   events; changed earlier values (most recent 3 periods) → `REVISED` events.

**First releases are never overwritten.** A revision is a new event
(`MACRO|<series>|<period>|REVISION|<vintage>`) whose `supersedes_event_id` points to the
latest earlier version available before it. Annual/benchmark revisions beyond the last
three periods are counted on the release event (`revised_observations_in_release`).

## 6. Deduplication and identity (`events/store.py`)

* `canonical_event_key` is unique (database constraint); `event_id` is a hash of it, so
  the same economic event has the same id in every database.
* Keys: SEC `SEC|<cik>|<accession>` (+`|METRICS`); FOMC `FOMC|DECISION|<date>`,
  `FOMC|MEETING|<date>`; macro as above.
* Re-fetching the same source → same key → no new row (idempotent). The same filing seen
  via different endpoints → same accession → same key. Corroborating sources are extra
  rows in `event_sources` (unique per event + source key), never extra events.
* Observed fields (values, times, status, period) are immutable after first storage; a
  conflicting re-ingestion is reported and the stored value kept. Derived fields
  (importance, channels, links, verification status) may be recomputed.
* Clusters: one economic event, several disclosures. An earnings 8-K (item 2.02) and the
  10-Q/10-K accepted within 3 days share `CL-EARN-<cik>-<8-K accession>`; indicators from
  one release share `CL-<group>-<date>`; FOMC `CL-FOMC-<date>`. Briefings rank a cluster
  once, by its highest-scoring member — repetition never adds importance.

## 7. Comparisons and surprise (`events/comparisons.py`)

| Kind | Formula | Unit |
|---|---|---|
| change_from_previous | level[t] − level[t−1] | index points / thousands of persons / percentage points (for rates) |
| pct_change_from_previous | (level[t]/level[t−1] − 1)·100 | percent change |
| annualized_pct_change (GDP) | ((level[t]/level[t−1])^4 − 1)·100 | percent change (SAAR) |
| yoy_pct_change / yoy_change | vs 12 months earlier | percent change / percentage points |
| percentile | share of prior ≤60 headline values ≤ current | requires ≥24 observations |
| standardized_change | (headline − mean)/sd over prior ≤60 | requires ≥24 observations, sd > 0 |
| revision | value[new vintage] − value[previous vintage] | level unit |
| rate_change (FOMC) | (upper[t] − upper[previous statement])·100 | basis points |
| surprise | actual − consensus | only with an authenticated `ConsensusObservation` published **before** the release, same unit |
| standardized_surprise | surprise / sd(historical surprises) | only with ≥20 historical surprises |

All comparisons use one vintage (what was known at that time). Momentum, revision and
surprise are distinct kinds. The previous value is **never** used as consensus. In v0.2
no consensus source exists: every event reports `consensus_status = unavailable` and
no surprise is computed.

## 8. Importance score (`events/importance.py`, method `importance-v0.2.1`)

```
importance_score = 100 × Σ(component values) / D
D = 100 (category 40 + magnitude 25 + extremeness 10 + novelty 10 + relevance 15)
  + 20 only when a surprise can be computed (authenticated consensus)
```

| Component | Max | Rule |
|---|---|---|
| category | 40 | judgmental base weight per subtype (rate change 40, rate hold 30, CPI/payrolls 34, core CPI 32, earnings 26, 10-K 16, 10-Q 14, Reg FD 6, …); **half weight for revisions** |
| magnitude | 25 | macro first release: 10 × \|standardized change\|; FOMC: 10 per 25 bp; revision: 10 × \|Δheadline\| / sd(headline history); metric change: 2 per pp change in revenue growth. Qualitative disclosures: not applicable → **contributes 0** |
| extremeness | 10 | 10 × ((\|percentile − 50\|)/50)²; 0 when no distribution exists |
| novelty | 10 | first publication 10, revision 4, amendment 3 |
| relevance | 15 | tracked company 15; economy-wide macro/central bank 12; otherwise 5 |
| surprise | 20 | 10 × \|standardized surprise\| — only with consensus; otherwise **removed from the denominator** |

Levels: HIGH ≥ 60, MEDIUM ≥ 40, LOW < 40. A quantitative event whose magnitude cannot be
measured is INSUFFICIENT_DATA and receives **no ranking score** (its partial score is kept in
the components). Missing consensus never lowers a score.

Calibration note: v0.2.0 normalized over *available* components only; on real data this let
qualitative filings (10-K 63 HIGH) outrank an FOMC hold (58) and let small-base payroll
revisions (relative-change magnitude) outrank a rate hike. v0.2.1 fixes both (fixed
denominator; sd-scaled, half-weight revisions) — see the regression tests.

**The weights are judgmental defaults chosen for interpretability; they have not been trained
or empirically validated.** `tests/fixtures/eval/ranking_expectations.json` holds curated
pairwise expectations on real events, but its labels were written by the same reasoning as
the weights (`independent_labels: false`), so they are regression checks only and **no
ranking metric is reported**. Every component, its maximum and a plain-language explanation
are stored on the event and shown in briefings ("Why this rank").

## 9. Economic transmission (`events/transmission.py`)

Channels record trigger event, source → target variable, direction, mechanism, supporting
evidence ids, interpretation type and limitations. v0.2 produces only
`ACCOUNTING_IDENTITY` (e.g. FCF = OCF − CAPEX) and `ECONOMIC_HYPOTHESIS` (e.g. faster
inflation → higher policy-rate expectations → higher short yields → pressure on long-
duration equities). Direction is conditional on the verified change (e.g. m/m inflation
vs the previous month). `HISTORICAL_STATISTICAL` and `OBSERVED_MARKET_REACTION` are reserved
for v0.3 and require data not present in v0.2. No coefficients, probabilities, confidence
intervals or price impacts are produced.

## 10. Briefing workflow (`workflows/briefing.py`)

Bounded LangGraph: parse → window → retrieve → resolve → compare → rank → select →
evidence → explain → verify → persist. The deterministic template narrative is always
produced; an LLM (optional) may add an executive summary without figures and
interpretations that must cite event ids and pass numeric screening, otherwise they are
dropped and listed. Verification checks: numeric claims (every figure must match stored
observations at displayed precision), evidence references, as-of compliance (every cited
event available within (window start, as_of]), occurrence status (no scheduled entry in
occurred sections), LLM screening, and scheduled-vs-published (scheduled releases in the
window without a confirmed publication are listed as uncertainty, never as events).
Failures are reported in the briefing, never silently discarded.

## 11. Historical replay requirements

* Ingestion stores each event with its `available_at`; queries filter
  `available_at ≤ as_of` (and window start <). A live historical ingestion with `as_of`
  additionally never stores post-cutoff events.
* Replay is deterministic from frozen snapshots: offline mode serves the recorded official
  responses (tests/fixtures; real snapshots built by `scripts/build_v02_fixtures.py`) and
  pins macro probing to the recorded window (`MANIFEST.json → event_window`).
* Tests cover: before / exactly at / after a release timestamp, revisions, repeated
  downloads, DST transitions, UTC vs New York boundaries, post-cutoff SEC filings,
  scheduled-but-unconfirmed releases, and negative tests that inject leakage, tampered
  numbers and schedules presented as occurrences.
