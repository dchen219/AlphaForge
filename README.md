# AlphaForge

*An agentic financial intelligence and quantitative research platform.*

**Released: v0.3.0b — Quantitative Event-Study Research Engine** (on v0.3.0a Historical Market
Data Infrastructure & CI, v0.2.0 Market Intelligence & Event Detection and v0.1's Financial
Research Foundation). AlphaForge is a modular,
local-first financial research platform. It retrieves official SEC EDGAR filings,
Federal Reserve communications, BLS/BEA release calendars and ALFRED vintage data,
normalizes them with full provenance, runs deterministic calculations, detects and ranks
financial events, and produces evidence-linked company research reports and daily
intelligence briefings via a FastAPI service and a CLI.

**Design rule: an LLM is never the source of a number.** Every figure in a report resolves
to an XBRL fact (accession, concept, unit, period, filing date) or a deterministic
calculation over such facts. LLM output (optional) is limited to interpretation, is screened
against the evidence, and is labelled separately from verified facts.

No trading, brokerage execution, or price prediction is implemented.

> **Naming:** the platform is branded AlphaForge; the Python package (`finance_agent`),
> distribution (`finance-agent`) and CLI command (`finance-agent`) keep their original
> names for compatibility.

## Roadmap

| Version | Milestone | Status |
|---|---|---|
| v0.1 | Financial Research Foundation — SEC/FRED data layer, provenance, deterministic fundamentals, evidence-linked company research | **Released** |
| v0.2 | Market Intelligence & Event Detection — macro/FOMC/SEC event ingestion, dedup, explainable importance, daily briefings | **Released (v0.2.0)** |
| v0.3-A | Historical market-data infrastructure — provider interface, versioned daily bars, corporate actions, adjustments, NYSE calendar, data quality, Treasury yields, CI | **Released (v0.3.0a)** |
| v0.3-B | Quantitative event-study engine — event-session alignment, market-adjusted/market-model abnormal returns, CAR inference, Treasury yield-change studies, confounder audit, multi-event aggregation, reproducible runs | **Released (v0.3.0b)** — methodological engine (synthetic equity prices; real yields) |
| v0.3 (C+) | Further event-to-asset impact research | Planned |
| v0.4 | Portfolio Intelligence | Planned |
| v0.5 | Quantitative Research Agents | Planned |

v0.3-C–v0.5 are not yet built. v0.3-B estimates *descriptive and statistical* event responses
(no causal identification, signals, trading, recommendations or portfolio optimization).

**What v0.3.0b is, and is not.** v0.3.0b is a *methodological* research engine:
- **Equity/ETF workflows** are validated end to end on SYNTHETIC prices. These are
  methodological checks against independent references, not empirical findings. Live Alpaca
  equity data is **not validated** because no credentials are available.
- **Treasury-yield observations** (DGS2/DGS10, ALFRED) are real and verified against a live vintage.
- **Inference** is classical, single-event (i.i.d. normal errors) with a placebo check. Results
  are `LIMITED` when diagnostics or confounders undermine its assumptions. There are no
  BMP/Kolari–Pynnönen/rank tests and no cross-asset clustered inference.
- **Confounding and coverage:** the confounder audit sees only ingested sources. Its coverage is
  inferred from stored events. News, analyst actions and consensus are not covered.
v0.3-A provides validated market data only. v0.2 deliberately stops short of prediction: no causal estimates,
price-impact forecasts, market reactions or consensus surprises (see
[docs/event_intelligence_methodology.md](docs/event_intelligence_methodology.md)).

## Quick start

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is installed by uv if needed.

```bash
uv sync                                   # install
cp .env.example .env                      # then set SEC_USER_AGENT (and optional keys)
uv run finance-agent init-db              # create/upgrade the SQLite DB (Alembic)

# Offline (no network, no keys): uses saved fixtures in tests/fixtures
uv run finance-agent financials MSFT --quarters 8 --offline
uv run finance-agent research MSFT --quarters 8 --offline --no-llm

# Live SEC data (requires SEC_USER_AGENT)
uv run finance-agent research MSFT --quarters 8
uv run finance-agent research -q "Analyze Microsoft's revenue growth, operating margins, free cash flow, and capital expenditure intensity over the most recent eight reported quarters."
uv run finance-agent research MSFT --as-of 2025-05-15      # historical, point-in-time
uv run finance-agent macro CPIAUCSL --as-of 2024-06-15     # requires FRED_API_KEY (or --offline)

# v0.2 events & daily briefings (no FRED key and no LLM needed)
uv run finance-agent events ingest --offline                     # replay REAL recorded official snapshots
uv run finance-agent briefing daily --date 2026-10-28 --no-llm --offline
uv run finance-agent events list --from 2026-10-01 --to 2026-11-01 --offline
uv run finance-agent events calendar --from 2026-11-01 --to 2026-11-30 --offline
uv run finance-agent events ingest --source sec --as-of 2026-11-01T12:00:00Z    # live (SEC_USER_AGENT)
uv run finance-agent briefing daily --date 2026-10-28 --no-llm                  # live ingestion + briefing

# v0.3-A market data (prices offline = SYNTHETIC fixtures; live needs ALPACA_API_KEY_ID/SECRET)
# On machines that block .exe launchers use: uv run python -m finance_agent <command>
uv run python -m finance_agent market instruments --offline
uv run python -m finance_agent market ingest --start 2024-01-02 --end 2026-10-30 --offline
uv run python -m finance_agent market prices NVDA --start 2024-06-03 --end 2024-06-14 --basis split_adjusted --offline
uv run python -m finance_agent market returns SPY --start 2025-01-02 --end 2025-12-31 --basis total_return --offline
uv run python -m finance_agent market quality --offline
uv run python -m finance_agent market calendar --date 2026-11-26
uv run python -m finance_agent market calendar --event-time 2026-10-28T16:05:00-04:00
uv run python -m finance_agent market yields DGS10 --start 2026-10-01 --end 2026-10-30 --offline   # REAL ALFRED data

# v0.3-B event studies (equity prices offline = SYNTHETIC -> methodological validation only)
uv run python -m finance_agent events ingest --offline --start 2025-07-01        # recorded REAL events (the default window is only the last 120 days)
uv run python -m finance_agent study run --event-id EV-e6a9f7badcb72f25 --ticker QQQ --benchmark SPY --model market-model --offline
uv run python -m finance_agent study run --event-id EV-e6a9f7badcb72f25 --series DGS10 --offline   # REAL yields, bp
uv run python -m finance_agent study compare --event-type MACRO_RELEASE --subtype CPI --ticker QQQ --benchmark SPY --offline
uv run python -m finance_agent study report --run-id ES-… --offline     # also: study list

# API
uv run finance-agent serve [--offline]    # http://127.0.0.1:8000/docs
```

CI: `.github/workflows/ci.yml` runs Ruff, migrations, the offline suite and the historical
replay tests on pull requests to `master` and on pushes to `master`, on GitHub-hosted Linux
runners, with no secrets.

Scheduling: v0.2 runs no daemon. `finance-agent briefing daily` (ingest + brief, exit code
non-zero on failure) is suitable for Windows Task Scheduler or a CI scheduler; nothing is
scheduled automatically.

Tests and lint:

```bash
uv run pytest                              # offline suite (live tests skipped)
RUN_LIVE_TESTS=1 LIVE_SEC_USER_AGENT="Name email" uv run pytest -m live
uv run ruff check . && uv run ruff format --check .
```

## Configuration

| Variable | Purpose |
|---|---|
| `SEC_USER_AGENT` | **Required for live SEC.** Descriptive UA with contact info (SEC fair-access policy). |
| `FRED_API_KEY` | FRED/ALFRED access. Without it, macro endpoints work only offline. |
| `ANTHROPIC_API_KEY` | Optional. Enables Claude (`ANTHROPIC_MODEL`, default `claude-opus-5-5`) for request interpretation and narrative. |
| `DATABASE_URL` | SQLAlchemy URL. Default SQLite in `./data`. Schema is PostgreSQL-compatible. |
| `OFFLINE_MODE` | `true` serves SEC/FRED from `tests/fixtures` (per-command: `--offline`). |
| `LOG_LEVEL`, `LOG_JSON` | Structured JSON logs to stderr; credentials are redacted. |
| `INGEST_ADMIN_TOKEN` | Enables `POST /intelligence/ingest` (loopback clients + matching `X-Admin-Token` only). Unset = disabled. |
| `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY`, `ALPACA_FEED` | v0.3-A live market data (Alpaca). Without keys, live market ingestion is refused; `--offline` uses SYNTHETIC fixtures. `ALPACA_FEED` = `sip` (default) or `iex`. |
| `MARKET_EXTREME_RETURN_THRESHOLD` | Daily move flagged for review (default 0.25); never deleted. |
| `EVENT_STUDY_TIME_BUDGET_SECONDS` | v0.3-B: wall-clock budget for one multi-event comparison (default 120; checked between events). |
| `MACRO_USE_CONFIRMED_SCHEDULED_TIME` | `true` (default): macro release available at the official scheduled time once ALFRED confirms the date; `false`: strict next-day policy. |

## API

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Status, DB check, which credentials are configured (booleans only) |
| GET | `/companies/{ticker}` | SEC profile and recent 10-K/10-Q filings |
| GET | `/companies/{ticker}/financials?quarters=8&as_of=` | Normalized quarterly values + calculations, with provenance |
| GET | `/macro/{series_id}?as_of=&start=&end=` | FRED/ALFRED observations with vintage metadata |
| POST | `/research/company` | `{"ticker"?, "question"?, "quarters": 8, "as_of"?, "use_llm": true}` → run + report |
| GET | `/research/{run_id}` | Stored run, steps, and report |
| GET | `/events?from=&to=&as_of=&category=&ticker=&level=&verification=&source=` | Events that became available in a window (point-in-time) |
| GET | `/events/important?as_of=&days=&min_level=` | Highest-importance events |
| GET | `/events/calendar?from=&to=` | Scheduled releases/meetings only — not occurrences |
| GET | `/events/{event_id}` | Event, its sources (with verbatim passages), cluster and superseded version |
| POST | `/intelligence/briefing` | `{"date"?, "as_of"?, "window_hours"?, "use_llm"}` → briefing |
| GET | `/intelligence/briefings/{briefing_id}` | Stored briefing |
| POST | `/intelligence/ingest` | Administrative ingestion; disabled unless `INGEST_ADMIN_TOKEN` is set |
| GET | `/market/instruments` | Research universe and yield series |
| GET | `/market/prices/{ticker}?start=&end=&basis=&mode=&as_of=&include_provisional=` | Daily bars on a stated basis (`raw`/`split_adjusted`/`total_return`), `point_in_time` (default) or retrospective `research` mode |
| GET | `/market/returns/{ticker}?start=&end=&basis=&kind=&window=` | Returns (basis **required**), cumulative, rolling volatility, drawdown, gaps |
| GET | `/market/correlations?tickers=&start=&end=&basis=` | Pairwise return correlations |
| GET | `/market/calendar?date=&from=&to=&event_time=` | NYSE sessions, early closes, event-to-session alignment |
| GET | `/market/yields/{series_id}?start=&end=&as_of=` | DGS2/DGS10 yields (percent) and basis-point changes |
| GET | `/market/quality?ticker=&check=&severity=` | Data-quality issues and recent ingestion runs |
| POST | `/market/ingest` | Administrative market ingestion; disabled unless `INGEST_ADMIN_TOKEN` is set (loopback + token) |
| GET | `/event-studies/specifications` | Models, bases, modes, default windows and request limits |
| POST | `/event-studies` | Run one event study `{"event_id", "ticker"\|"series_id", "benchmark", "model", "price_basis", "data_mode", "as_of"?, "windows"?, …}` — administrative (loopback + `X-Admin-Token`), bounded |
| POST | `/event-studies/compare` | Bounded multi-event comparison (≤ 100 events, time budget) — administrative |
| GET | `/event-studies` · `/event-studies/{run_id}` · `/{run_id}/results` · `/{run_id}/report?format=json\|markdown` | Stored immutable runs, per-window results, 14-section report |

A bare date in `from` means 00:00 New York; in `to`/`as_of`/`date` it means 23:59:59 New York.

## Architecture

A modular monolith with strictly separated layers:

```
Interface      interfaces/api.py (FastAPI) · interfaces/cli.py (Typer)
Orchestration  workflows/company_research.py — bounded linear LangGraph:
               parse → resolve → plan → retrieve → select periods → calculate
               → trends → evidence → synthesize → verify → persist   (any error → fail)
Agent          agents/company_research.py (request parsing, evidence-bound synthesis)
               agents/llm.py (pluggable provider: Anthropic | Null)
Verification   verification/checks.py (references, numeric claims, recomputation,
               provenance, as-of leakage, coverage, LLM screening)
Quantitative   research/calculations.py · research/trends.py
Normalization  research/normalize.py (period-aware XBRL selection) · research/concepts.py
Retrieval      retrieval/evidence.py (evidence registry: facts, calcs, trends, filings)
Storage        storage/models.py (SQLAlchemy 2) · migrations (Alembic) · snapshots.py
               (immutable SHA-256 content store) · repository.py
Providers      providers/sec.py · providers/fred.py · http.py (retry, rate limit, fixtures)

v0.2 event intelligence (events/)
Sources        events/sources/{sec_events,fomc,macro,calendars}.py
Normalization  events/schemas.py (controlled taxonomy) · events/store.py (dedup, immutability,
               point-in-time queries, briefing persistence)
Intelligence   events/comparisons.py · events/importance.py · events/transmission.py
Briefing       events/briefing.py · workflows/briefing.py (bounded LangGraph) ·
               interfaces/events_api.py · CLI `events` / `briefing`

v0.3-A market data (market/)
Provider       market/providers.py (MarketDataProvider interface · AlpacaProvider)
Ingestion      market/ingest.py (bounded, incremental, idempotent) · market/yields.py (ALFRED)
Storage        market/store.py (versioned bars/actions, point-in-time reads, issues, runs)
Rules          market/calendar.py (exchange_calendars XNYS) · market/adjust.py · market/quality.py
Queries        market/calculations.py · market/service.py · interfaces/market_api.py · CLI `market`

v0.3-B event studies (event_studies/)
Alignment      alignment.py (daily-close-v1) · windows.py (trading-session offsets)
Returns        returns.py (only via the v0.3-A MarketDataService) · benchmarks.py
Estimation     market_model.py (statsmodels OLS) · inference.py (Var(CAR) with estimation error,
               placebo) · diagnostics.py
Studies        engine.py (single event) · yields_study.py (bp) · confounders.py · aggregation.py
Runs           service.py · store.py (immutable, content-addressed) · reports.py (JSON + Markdown)
               · interfaces/studies_api.py · CLI `study`
```

Database tables added in v0.3-B (migration `0006_event_studies`): `event_study_runs`,
`event_study_results`. Methodology: [docs/event_study_methodology.md](docs/event_study_methodology.md),
validation: [docs/event_study_statistical_validation.md](docs/event_study_statistical_validation.md),
data requirements: [docs/event_study_data_requirements.md](docs/event_study_data_requirements.md).

Database tables added in v0.3-A (migrations `0004_market_data`, `0005_market_version_identity`): `market_instruments`,
`market_provider_mappings`, `market_price_bars`, `market_corporate_actions`,
`market_trading_sessions`, `market_quality_issues`, `market_ingestion_runs`,
`market_yield_observations`. Methodology: [docs/market_data_methodology.md](docs/market_data_methodology.md),
[docs/trading_calendar_methodology.md](docs/trading_calendar_methodology.md), provider choice:
[docs/market_data_provider_evaluation.md](docs/market_data_provider_evaluation.md).

Database tables added in v0.2 (migration `0003_event_intelligence`): `financial_events`,
`event_sources`, `briefings`, `briefing_events` — see [docs/database_schema.md](docs/database_schema.md).

Deviations from the suggested layout: the FastAPI app factory lives in
`src/finance_agent/interfaces/` so it is importable and testable from the package;
`apps/api/main.py` is a thin entry point. Alembic migrations live inside the package
(`storage/migrations`) so `init-db` works from an installed wheel.

### Key data rules (details: [docs/accounting_methodology.md](docs/accounting_methodology.md))

- **Point-in-time**: date-stamped sources are usable from the next day 00:00 New York.
  Filings/vintages published after `as_of` are excluded; later comparatives and
  restatements are invisible to earlier `as_of` runs.
- **Duplicates/restatements**: latest eligible filing wins; differing values are flagged
  with alternatives.
- **Standalone quarters**: reported 3-month facts, or YTD differences only when start
  dates match and the gap is exactly one quarter. No other derivations.
- **CAPEX**: cash purchases of PP&E (fallback tag flagged); excludes finance leases,
  acquisitions, intangibles. FCF = OCF − CAPEX.
- **Debt**: balance-sheet carrying components (`LongTermDebtNoncurrent + LongTermDebtCurrent`)
  preferred; `LongTermDebt` (often face value) only as a flagged fallback; leases excluded.
- **`as_of` input**: a bare date means 23:59:59 New York in the CLI, query strings and
  the POST body alike.
- **Missing data is reported, never filled.** E.g. no gross profit is fabricated for
  companies that don't report it; the gross-margin fallback via cost of revenue is flagged.

### Provenance

Every raw HTTP response is stored as an immutable gzip snapshot addressed by SHA-256
(`data/snapshots`), re-verified on read, and referenced by DB rows. Only retrieval
metadata (`last_retrieved_at`) changes on an identical re-fetch, which keeps the cache TTL
accurate. Writes are idempotent and retried on unique-key conflicts; SQLite runs in WAL mode. Each normalized value
carries its source facts (accession, form, filing date, availability time, concept, unit,
period, EDGAR URL). Fresh snapshots are reused (TTL) to minimise SEC requests.

## Data origins — real vs fixture

| Origin label | Meaning |
|---|---|
| `live` | Retrieved from SEC/FRED during this run |
| `fixture:real_snapshot` | `tests/fixtures/sec`: **real** SEC data saved 2026-11-02 UTC, trimmed (see `MANIFEST.json`) |
| `fixture:synthetic` | `tests/fixtures/fred`: **synthetic** FRED-format data — not real macro data |
| `fixture:synthetic` (v0.3-A) | `tests/fixtures/alpaca`: **synthetic** daily bars/corporate actions in the Alpaca response format (seeded random walk) — **not market data**; CLI/API outputs say so |
| `fixture:real_snapshot` (v0.3-A) | `tests/fixtures/alfred/DGS2`, `DGS10`: **real** ALFRED Treasury-yield vintages incl. consecutive-day holiday calibration windows |
| `fixture:real_snapshot` (v0.2) | `tests/fixtures/{fed,bls,bea,alfred}` and refreshed `sec/submissions`: **real** official responses recorded by `scripts/build_v02_fixtures.py` running the actual ingestion code (window 2025-07-01 → 2026-11-01) |

Reports, briefings and event studies print the origin in a banner (any synthetic input makes an
event study `synthetic: true`). Example outputs: [docs/examples](docs/examples)
(`MSFT_live_report.md` and `briefing_2026-10-28_live.md` from live official sources;
`briefing_2026-10-*_offline_real_snapshot.md` from replayed real snapshots).

## Limitations

See IMPLEMENTATION_LOG.md → "Limitations" and CLAUDE.md for the full list. Highlights: live FRED and
live LLM synthesis were not verifiable in the build environment; only XBRL financial
statement data is analysed (no MD&A text, segments, guidance or prices); custom
company-specific XBRL extension concepts are ignored; research requests run synchronously.
v0.2: no consensus-forecast or market-price source, so no surprises or market reactions are
reported; importance weights are judgmental (not empirically validated); macro release
*times* use the official schedule with the *date* confirmed by ALFRED; calendars are not
versioned historically (historical briefings withhold calendar entries retrieved after their
cutoff); 8-K press-release text is not parsed (classification uses SEC item
metadata only); live LLM synthesis unverified (no API credit).
v0.3-A: live Alpaca market data is unverified (no credentials; offline prices are synthetic);
delisted-security coverage and provider correction behaviour are unverified; no
corporate-action announcement dates (actions known from their ex-date); a full-day trading halt
is indistinguishable from missing data without a halt source; the historical backfill assumes
first-retrieved values equal the originally published ones.
v0.3-B: no empirical equity event study is possible without live price credentials (all equity
results here use SYNTHETIC prices); single-event inference is classical (normal, i.i.d.) with a
placebo check — no BMP / Kolari–Pynnönen / rank tests, no cross-asset clustered inference;
daily data only (no intraday reactions); confounder coverage is inferred from stored events;
point-in-time studies over backfilled bars are reported UNVERIFIED; no causal identification.
