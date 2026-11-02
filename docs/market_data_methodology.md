# Market-data methodology (v0.3-A)

Implementation: `src/finance_agent/market/`. Tables: `docs/database_schema.md` (migration
`0004_market_data`). Provider choice: `docs/market_data_provider_evaluation.md`. Calendars:
`docs/trading_calendar_methodology.md`.

## 1. Raw vs normalized storage

* **Raw:** every provider response is an immutable, SHA-256-addressed source snapshot
  (`source_snapshots`, gzip on disk, hash re-verified on read). These files are never committed.
* **Normalized:** one `market_price_bars` row per *distinct* provider observation, with:
  - instrument, provider and provider symbol, plus exchange when the provider reports one;
  - session date, provider timestamp (UTC) and session timezone;
  - OHLCV, VWAP and trade count;
  - adjustment basis, currency and frequency;
  - snapshot id and hash, data origin, retrieval time;
  - availability time and its precision;
  - bar status (FINAL / PROVISIONAL) and quality status (OK / WARNING / QUARANTINED);
  - version.
* **Versions, never overwrites:** re-ingesting an identical observation is a no-op
  (`content_hash`). A different value becomes version n+1, and earlier versions are kept.

## 2. Timestamps and availability

| Concept | Field | Daily-bar policy |
|---|---|---|
| Market timestamp | `bar_timestamp` | as reported (Alpaca daily bars: 00:00 New York of the session, in UTC) |
| Trading-session date | `session_date` | New York date of the timestamp; must be an XNYS session |
| Retrieval time | `retrieved_at` | when the snapshot was fetched (offline: the MANIFEST recording time) |
| Availability | `available_at` | **D+1 00:00 America/New_York** (`DATE_ONLY_CONSERVATIVE`). The provider publishes no finalization time, so no intraday time is invented. |
| Correction time | `available_at` of a later version | the retrieval time at which the change was first observed (`OBSERVED_AT_RETRIEVAL`) |
| Corporate-action effective date | `ex_date` (mergers: effective date) | the action is treated as known from **ex-date 00:00 New York**. Alpaca's schema has no announcement date, and none is invented. |

**Provisional bars.** A bar retrieved before D+1 00:00 New York may still change. It is stored
`PROVISIONAL` with `available_at = retrieved_at`, excluded from point-in-time queries and every
calculation by default, and shown only with `include_provisional`, labelled. A later retrieval
supplies the FINAL version.

**Backfill caveat.** The first ingestion of history sees today's version of each past bar. Any
correction made before that retrieval cannot be recovered, so the first version is *assumed* to
equal what was available at D+1. Only corrections observed later are point-in-time exact.

## 3. Query modes (different output contracts)

| Mode | Bars | Corporate actions | Statement in output |
|---|---|---|---|
| `point_in_time` (default; `as_of` defaults to now) | latest version with `available_at ≤ as_of`, FINAL and not quarantined | only those with `available_at ≤ as_of` and `ex_date ≤ as_of` | "POINT-IN-TIME view …" |
| `research` (no `as_of` allowed) | latest version of everything | all known now | "RETROSPECTIVE research view … not necessarily what an investor saw" (`retrospectively_adjusted: true`) |

Retrospectively adjusted historical prices incorporate corporate actions that happened after
those dates. They are never described as what a historical investor saw.

## 4. Adjustment definitions (`market/adjust.py`, `adjustment-v0.3a.1`)

Backward adjustment: the latest prices are unchanged and earlier sessions are scaled.

* **Raw:** the provider's unadjusted prices. Never adjusted.
* **Split-adjusted:** for a split with ratio *r* = new/old (10-for-1 → 10; 1-for-10 reverse → 0.1),
  every session strictly before the ex-date has prices × 1/*r* and volume × *r*.
* **Total-return-compatible:** split factors, plus for each cash dividend *D* with ex-date *E*, a
  factor *f* = 1 − *D* / *C* applied to every session before *E*.
  - *C* is the raw close of the last session before *E*, expressed in ex-date share terms
    (split-adjusted for any split between that session and *E*).
  - This is a CRSP-style reinvestment factor.
  - If *C* is missing, or *D* ≥ *C*, no factor is applied and the response carries a note.
* Each factor records its source action id, provider, method and the reference close/session.
* **Provider-adjusted bars** (`provider:split`, `provider:all`) are stored as separate bases for
  reconciliation and are **never re-adjusted**, so data cannot be double-adjusted.
  `adjust.reconcile` compares the internal and provider series. The offline test reconciles
  synthetic NVDA provider split bars over more than 700 sessions within a 1e-4 relative
  tolerance. This checks the code path only; it is not independent evidence about live data.
* Adjustment uses only stored corporate actions. Missing actions are never inferred. A
  split-sized jump without a recorded action is flagged (`UNEXPLAINED_DISCONTINUITY`), not
  "fixed".

## 5. Calculations (`market/calculations.py`)

Calculations take an explicit basis (`raw | split_adjusted | total_return`). The API and CLI
reject anything else; there is no default substitution for returns.

* **Simple returns:** *Pₜ/Pₜ₋₁* − 1. **Log returns:** ln(*Pₜ/Pₜ₋₁*). Both are computed only
  between *consecutive XNYS sessions* that both have a FINAL, non-quarantined close.
* **Missing session:** the returns into and out of it are `null` with a reason. A nearby price is
  never used.
* **Raw basis across a split ex-date:** refused (`null`, "not an economic return"). Drawdown is
  not computed on the raw basis across a split.
* **Cumulative return:** requires an unbroken chain; otherwise `null` with the gaps listed.
* **Rolling volatility:** sample standard deviation of daily log returns × √252, computed only
  for full windows.
* **Drawdown:** from the running peak of the chosen basis.
* **Correlation:** Pearson, on sessions where both returns are valid; at least 20 pairs.
* **Treasury yield changes:** in basis points, (*yₜ* − *y*prev) × 100 (1 percentage point =
  100 bp), measured against the previous *published* observation, whose date is reported.

No predictive models, signals or event-study regressions are part of v0.3-A.

## 6. Missing-session handling and data quality (`market/quality.py`)

| Check | Action |
|---|---|
| Missing required field, non-positive price, negative volume, high < low, open/close outside [low, high] (relative tolerance 1e-6) | **QUARANTINED**: stored for audit, excluded everywhere |
| Bar on a weekend/holiday (`NON_SESSION_BAR`) | QUARANTINED |
| Timestamp not 00:00 New York of its session (`WRONG_TIMEZONE`) | WARNING; quarantined if it maps to a non-session |
| Duplicate bars in one response | identical → collapsed (INFO); conflicting → all quarantined (ERROR), neither chosen |
| Provider symbol mapped to two instruments | ERROR |
| Daily move beyond ±25 % (configurable, `MARKET_EXTREME_RETURN_THRESHOLD`) | WARNING, **kept** (may be real) |
| Raw jump at a recorded split ex-date | INFO, `EXPLAINED` |
| Split-sized jump with no recorded action | WARNING (`UNEXPLAINED_DISCONTINUITY`) |
| Raw-labelled series with no jump at a recorded split, or an adjusted series with a split-sized jump | WARNING (`INCONSISTENT_ADJUSTMENT`) |
| Provider changed a finalized bar | INFO (`CORRECTION`); both versions kept |
| Invalid corporate action (e.g. a non-positive split rate) | rejected, not stored; ERROR |

Gap classification (a gap is never filled):

| Gap kind | Meaning |
|---|---|
| `MARKET_CLOSED` | the calendar has no session; not reported |
| `MISSING_DATA` | the provider answered but returned no bar. A full-day trading halt cannot be distinguished without halt data, and the message says so. |
| `PROVIDER_FAILURE` | the request failed |
| `NOT_FINALIZED` | only a provisional bar exists |
| `QUARANTINED` | the only bar is invalid |

`TRADING_HALT` is reserved; v0.3-A has no halt source. Gaps are evaluated inside the observed
span of each instrument, so periods before listing are not flagged.

## 7. Treasury yields (`market/yields.py`)

Series DGS2 and DGS10 (H.15 constant-maturity yields; percent per annum, **rates, not prices**)
come from keyless ALFRED vintage CSVs (`alfredgraph.csv?id=…&vintage_date=…&cosd=…&coed=…`;
crawl-delay 2 s → 0.5 req/s). Missing observations (`.`) are stored as null.

**Availability rule.** An observation for day *t* first appears in the vintage of the next H.15
publication day:
- Publication days are weekdays except US federal holidays as observed by the federal government
  (a Saturday holiday is observed Friday, a Sunday holiday Monday) and executive-order closures.
- Holiday rows (published as missing) appear one publication day later still.
- Because ALFRED vintages carry a date and no time, the observation is available from 00:00 New
  York on the day after that first vintage (`RULE_BASED_CONSERVATIVE`).

**Calibration on real data.** Recorded consecutive-day vintages around nine holiday windows in
2025–2026:

| Result | Per series |
|---|---|
| Rule exactly matches the real first vintage | 35 of 38 |
| Rule is one day late (conservative) | 3 of 38 |
| Rule is earlier than the real first vintage | **0** |

The first rule tried ("next Federal Reserve business day") was **early in 10 of 38 cases**:
- Independence Day 2026 fell on a Saturday, and nothing was published on Friday 2026-07-03.
- No new observations appeared in the 2025-12-24 and 2025-12-26 vintages.
- Holiday rows were published two days later.

It was replaced; `tests/historical_replay/test_market_replay.py` keeps the calibration as a
regression test.

A later vintage that changes a value creates a new version, available from the day after that
vintage. Daily H.15 values are end-of-day market quotes and do **not** measure an immediate
reaction to an intraday announcement.

## 8. Reproducibility

```bash
uv sync --locked
uv run python scripts/build_v03_fixtures.py                 # regenerate SYNTHETIC Alpaca-format fixtures (deterministic)
uv run python scripts/build_v03_fixtures.py --no-synthetic --record-yields   # re-record REAL ALFRED vintages (~5 min)
uv run python -m finance_agent market ingest --start 2024-01-02 --end 2026-10-30 --offline
uv run python -m finance_agent market quality --offline
uv run python -m pytest tests/historical_replay
```

## 9. Licensing

Provider market data is licensed, so none is committed. The repository contains only:
- **synthetic** price fixtures, generated by a seeded random walk (`MANIFEST.json` `alpaca/`:
  `kind: synthetic`);
- **real** ALFRED Treasury-yield vintages, which are Federal Reserve H.15 data via the St. Louis
  Fed.
