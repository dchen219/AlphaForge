# Event-study data requirements (v0.3-B)

## Events (v0.2 event store)

- **The event must have occurred.** It must be `PUBLISHED` or `REVISED`; calendar entries
  (`SCHEDULED`) are refused.
- **Publication timing:** a verified timestamp (EDGAR/Fed), an official scheduled time with a
  source-confirmed date (BLS/BEA), or a date only (uncertain-time rules). Events with no usable
  time are ineligible.
- **Point-in-time studies:** the event's `available_at` must be at or before the cutoff.
- **Confounder audit:** it is only as complete as the sources ingested. Coverage is inferred per
  source family and reported.

## Prices (v0.3-A market store)

- The target and benchmark must be in the market-data universe and ingested on the same basis.
- **Coverage:** final, non-quarantined daily bars for every session from estimation start - 1
  through the last event-window session. The defaults need 151 sessions before day 0 and 5 after.
  Point-in-time studies also need them to be final at the cutoff.
- **Basis:** explicit `raw`, `split_adjusted` or `total_return`. Raw studies cannot span a split
  ex-date; dividends are only in `total_return`.
- **Point-in-time verification:** bars must have been **retrieved** at or before the cutoff.
  Backfilled history (first retrieved later) makes a point-in-time study `UNVERIFIED`, because
  the original provider snapshots of that time do not exist.

## Treasury yields (ALFRED, keyless)

- DGS2/DGS10 vintages covering estimation start - 1 through the last window day.
- In percent; changes are reported in basis points.
- Point-in-time studies use only vintages available at the cutoff, under the v0.3-A
  first-vintage rule.

## Data origins in this repository

| Input | Origin | Status of results |
|---|---|---|
| Events (`tests/fixtures/{sec,fed,bls,bea,alfred}`) | REAL recorded official snapshots | real |
| Equity/ETF prices (`tests/fixtures/alpaca`) | **SYNTHETIC** seeded random walk in the Alpaca format | **methodological validation only, never empirical findings** |
| Treasury yields (`tests/fixtures/alfred/DGS*`) | REAL ALFRED vintages | real (descriptive) |
| Live ALFRED | live | real (descriptive) |
| Live Alpaca | **not available** (no credentials) | — |

Any synthetic input makes the run `fixture:synthetic` / `synthetic: true` with a banner. Results
on live or recorded data carry their own origin (e.g. `mixed:fixture:real_snapshot+live`).

## What is needed for empirical equity validation

1. Create a free Alpaca account; AlphaForge does not create accounts or buy subscriptions.
   Then set `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY` in `.env`.
2. Verify the feed: `ALPACA_FEED=sip` (consolidated) vs `iex` (partial volume, not consolidated
   market data).
3. Run the live tests:
   `RUN_LIVE_TESTS=1 LIVE_ALPACA_API_KEY_ID=… LIVE_ALPACA_API_SECRET_KEY=… uv run python -m pytest -m live`.
4. Ingest: `uv run python -m finance_agent market ingest --start 2024-01-02 --end <today>`.
   Then check the history depth, daily-bar timestamps, splits (e.g. NVDA 2024-06-10) and
   dividends.
5. Run studies; results will then carry `live` origins.

Raw provider responses stay in the local snapshot store and are never committed (licensing).
Scraped or unlicensed price sources must not be substituted.

## Reproducing the offline examples

```bash
uv run python -m finance_agent events ingest --offline --start 2025-07-01   # the recorded event window
uv run python -m finance_agent market ingest --start 2024-01-02 --end 2026-10-30 --offline
uv run python -m finance_agent study run --event-id EV-e6a9f7badcb72f25 --ticker QQQ --benchmark SPY --model market-model --offline
uv run python -m finance_agent study run --event-id EV-e6a9f7badcb72f25 --series DGS10 --offline
uv run python -m finance_agent study compare --event-type MACRO_RELEASE --subtype CPI --subtype CORE_CPI --ticker QQQ --benchmark SPY --offline
uv run python -m finance_agent study report --run-id ES-… --offline
```
