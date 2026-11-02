# CLAUDE.md — AlphaForge

AlphaForge: *an agentic financial intelligence and quantitative research platform.*
(Formerly "Finance Agent". The Python package `finance_agent`, distribution and CLI
command `finance-agent`, and database schema intentionally keep their original names —
do not rename them.)

Guidance for AI coding agents and contributors working in this repository.
Status: **v0.3.0a released** on `master` (v0.1 Financial Research Foundation, v0.2 Market
Intelligence & Event Detection [tag `v0.2.0`], v0.3-A Historical Market Data Infrastructure & CI
[PR #2, tag `v0.3.0a`]). **v0.3-B — Quantitative Event-Study Research Engine** implemented on
branch `feat/v0.3b-event-study` (in review; not merged). Roadmap: v0.3-C+ further event-to-asset
research · v0.4 Portfolio Intelligence · v0.5 Quantitative Research Agents (not started — do not
start without an explicit request). Event-study rules: `docs/event_study_methodology.md`,
`docs/event_study_statistical_validation.md`, `docs/event_study_data_requirements.md`. Market-data rules:
`docs/market_data_methodology.md`, `docs/trading_calendar_methodology.md`. Event semantics: `docs/event_intelligence_methodology.md`. Detailed history: `IMPLEMENTATION_LOG.md`. Accounting rules:
`docs/accounting_methodology.md` (source of truth for data semantics).

## What this is

A local-first, modular-monolith financial research agent: SEC EDGAR + FRED/ALFRED data
→ provenance-preserving normalization → deterministic calculations → evidence-linked
company research reports, exposed via FastAPI and a Typer CLI.

Non-goals (v0.1): trading, brokerage execution, price prediction, vector databases,
Redis/Celery/microservices, frontend.

## Inviolable rules

1. **An LLM is never the source of a number.** Figures come from XBRL facts or
   deterministic calculations. LLM text is interpretation only, is screened by
   `verification/checks.py`, and is labelled `model_interpretation`.
2. **No look-ahead.** Anything used for an `as_of` run must satisfy
   `available_at <= as_of` (policy below). Never use latest-revised data historically.
3. **Missing data is reported, never filled or forced.**
4. **Never present fixture or synthetic data as real.** Every response carries a data
   origin (`live`, `fixture:real_snapshot`, `fixture:synthetic`).
5. **No secrets in code, logs, fixtures or Git.** Settings use `SecretStr`; logs pass
   through `config/logging.py:redact`.
6. **Source snapshots are immutable.** Content is addressed by SHA-256 and re-verified
   on read; only retrieval metadata (`last_retrieved_at`) may change.
7. **(v0.2) A calendar entry is never an occurrence.** `SCHEDULED` events carry no values and
   are excluded from occurred-event queries; a passed date without source confirmation is
   `UNCONFIRMED`, never "happened".
8. **(v0.2) No surprise without authenticated consensus; no market reaction without market
   data.** The previous value is never a stand-in for consensus. Economic channels are
   labelled hypotheses; no coefficients, probabilities or price impacts.
9. **(v0.2) Observed event facts are immutable.** Revisions are new events that supersede
   earlier ones; first releases are preserved.
10. **(v0.3-A) Market observations are versioned, never overwritten.** A provider correction is
    a new version visible only from when it was observed. Provisional (unfinished-session)
    bars are never presented as final or used in calculations.
11. **(v0.3-A) Price basis is always explicit.** Raw, split-adjusted and total-return are
    different series; returns never span a split on the raw basis; provider-adjusted bars are
    never re-adjusted. Point-in-time queries use only corporate actions available at the
    cutoff; retrospective (`research`) output is labelled as such.
12. **(v0.3-A) Gaps are never filled.** Missing sessions are classified (closure / missing
    data / provider failure / not finalized / quarantined) and reported. Extreme moves are
    flagged, never deleted. Treasury yields are rates (percent; changes in bp), not prices.
13. **(v0.3-B) An event study never claims causality.** Results separate descriptive movement,
    benchmark-adjusted association, statistical significance and (never established) causal
    identification. Daily close-to-close returns are not intraday reactions; regular-session
    releases are flagged as contaminated.
14. **(v0.3-B) Timing and data contracts are explicit.** No publication time is invented
    (date-only events make `[0,0]` ineligible); `point_in_time` studies never fall back to
    retrospective data and are `UNVERIFIED` when bars were retrieved after the cutoff; any
    synthetic input makes the whole run `synthetic: true` with a banner.
15. **(v0.3-B) Windows are never shortened or filled; dependence is handled conservatively.**
    Events released together count once, overlapping windows are clustered, cross-sectional
    tests are `LIMITED` and need >= 10 independent clusters. Runs are immutable and
    content-addressed (spec hash + data digest); changed inputs make a new run.

## Stack

Python 3.12 (uv) · FastAPI · Pydantic v2 / pydantic-settings · SQLAlchemy 2.x + Alembic ·
SQLite (PostgreSQL-compatible schema) · httpx · LangGraph (bounded linear workflow) ·
Anthropic SDK (`claude-opus-5-5`) · Typer/Rich · pytest · Ruff. `tzdata` is required on
Windows. pandas/NumPy are not used for v0.1–v0.3-A calculations (scalar, auditable arithmetic);
since v0.3-A they arrive as dependencies of `exchange_calendars` (`market/calendar.py`). v0.3-B
`event_studies/` uses NumPy, SciPy (`scipy.stats`) and statsmodels (OLS, Ljung–Box, Jarque–Bera).
Import statsmodels **submodules only** (`statsmodels.regression.linear_model`,
`statsmodels.stats.diagnostic`, …): `statsmodels.api` pulls in `scipy.spatial`, whose DLL is
blocked by Windows Application Control on the development machine.

## Layout

```
src/finance_agent/
  config/       settings (env), structured JSON logging + redaction
  http.py       retries/backoff, Retry-After, per-host rate limits, FixtureTransport (offline)
  providers/    sec.py (tickers, submissions, companyfacts), fred.py (ALFRED vintages), base.py (snapshot cache)
  storage/      models.py, db.py (engine, WAL, retry_on_conflict), snapshots.py, repository.py, migrations/
  research/     concepts.py (XBRL mapping), normalize.py (periods), calculations.py, trends.py, report.py
  retrieval/    evidence.py (evidence registry: V-/C-/T-/F- ids)
  verification/ checks.py (references, numeric claims, recomputation, provenance, as-of, coverage)
  agents/       llm.py (Anthropic | Null provider), company_research.py (parsing, synthesis)
  workflows/    company_research.py (LangGraph: parse→resolve→plan→retrieve→periods→calc→trends→evidence→synthesize→verify→persist; errors→fail)
  interfaces/   api.py, cli.py, asgi.py
  services.py   AppContext: wires providers/storage/LLM for API, CLI and workflow
apps/api/main.py   thin ASGI entry
  events/       v0.2: schemas (taxonomy), store (dedup/immutability/point-in-time), comparisons,
                importance, transmission, briefing, service (ingestion), sources/{sec_events,fomc,macro,calendars}
  workflows/briefing.py   bounded LangGraph: parse→window→retrieve→resolve→compare→rank→select→evidence→explain→verify→persist
  interfaces/events_api.py  /events*, /intelligence/* routes
  market/       v0.3-A: schemas, providers (MarketDataProvider, AlpacaProvider), ingest, store
                (versioned bars/actions, point-in-time reads), calendar (exchange_calendars XNYS),
                adjust, quality, calculations, yields (ALFRED DGS2/DGS10), service (query modes)
  interfaces/market_api.py  /market/* routes
  event_studies/ v0.3-B: schemas (spec + hashes), alignment (daily-close-v1), windows, returns (via
                MarketDataService only), benchmarks, market_model (statsmodels OLS), inference
                (Var(CAR) incl. estimation error, placebo), diagnostics, engine (single event),
                yields_study (bp, ~15:30 ET rule), confounders, aggregation, reports, store, service
  interfaces/studies_api.py /event-studies* routes (POST = admin: loopback + token)
.github/workflows/ci.yml   CI: ruff, migrations, offline suite, replay (no secrets)
tests/{unit,integration,historical_replay,fixtures}
scripts/build_fixtures.py       v0.1 fixtures (SEC real, FRED synthetic)
scripts/build_v02_fixtures.py   records REAL v0.2 fixtures by running ingestion live (~15 min)
scripts/build_v03_fixtures.py   SYNTHETIC Alpaca-format bars (deterministic) + --record-yields REAL ALFRED (~5 min)
```

## Commands

```bash
uv sync
uv run pytest                                   # offline suite; live tests skip
uv run pytest tests/historical_replay           # look-ahead tests
RUN_LIVE_TESTS=1 LIVE_SEC_USER_AGENT="Name email" [LIVE_FRED_API_KEY=…] [LIVE_ANTHROPIC_API_KEY=…] uv run pytest -m live
uv run ruff check . && uv run ruff format --check .
uv run alembic upgrade head && uv run alembic check   # uses DATABASE_URL
uv run finance-agent init-db
uv run finance-agent financials MSFT --quarters 8 [--as-of YYYY-MM-DD] [--offline]
uv run finance-agent research MSFT --quarters 8 [--as-of …] [--no-llm] [--offline] [--format json]
uv run finance-agent macro CPIAUCSL [--as-of …] [--offline]
uv run finance-agent serve [--offline]          # http://127.0.0.1:8000/docs
```

v0.2:

```bash
uv run finance-agent events ingest [--source sec|fomc|macro|all] [--as-of …] [--start …] [--offline]
uv run finance-agent events list --from 2026-10-01 --to 2026-11-01 [--as-of …] [--offline]
uv run finance-agent events show EV-… | events calendar --from … --to …
uv run finance-agent briefing daily --date 2026-10-28 [--no-llm] [--offline] [--no-ingest] [--format json]
```

v0.3-A (use `uv run python -m finance_agent …` where .exe launchers are blocked; same for
`uv run python -m pytest`):

```bash
uv run python -m finance_agent market ingest --start 2024-01-02 --end 2026-10-30 [--ticker T …] [--offline] [--refresh] [--no-yields] [--provider-adjusted]
uv run python -m finance_agent market prices NVDA --start … --end … [--basis raw|split_adjusted|total_return] [--mode point_in_time|research] [--as-of …] [--include-provisional]
uv run python -m finance_agent market returns SPY --start … --end … --basis total_return [--kind log] [--window 20]
uv run python -m finance_agent market quality | market calendar --date 2026-11-26 | market yields DGS10 --start … --end …
```

Live market data needs `ALPACA_API_KEY_ID` + `ALPACA_API_SECRET_KEY` (unverified — no keys at
build time). Offline prices are SYNTHETIC; offline yields are REAL (MANIFEST `yield_window`).

v0.3-B (offline events need `--start 2025-07-01`, the recorded window):

```bash
uv run python -m finance_agent study run --event-id EV-… --ticker QQQ --benchmark SPY --model market-model [--basis total_return] [--mode research|point_in_time --as-of …] [--window 0:1 …] [--no-robustness] [--offline] [--format markdown|json]
uv run python -m finance_agent study run --event-id EV-… --series DGS10 [--offline]
uv run python -m finance_agent study compare --event-type MACRO_RELEASE [--subtype CPI …] --ticker QQQ --benchmark SPY [--start … --end …] [--max-events 40] [--offline]
uv run python -m finance_agent study report --run-id ES-… | study list
```

Live SEC requires `SEC_USER_AGENT` (name + contact). Offline mode serves `tests/fixtures`
(and, in v0.2, replays macro probing over the recorded window in `MANIFEST.json → event_window`;
rate limits are not applied offline). v0.2 needs neither a FRED key (ALFRED keyless CSV) nor
an Anthropic key.

## Source availability policy (`timeutil.py`)

- SEC companyfacts `filed` and ALFRED `realtime_start` are **dates only**. A source dated
  D is usable from **D+1 00:00 America/New_York** (EDGAR disseminates until 22:00 ET).
  Same-day use is impossible by design. `acceptanceDateTime` is stored, **not** used.
- `as_of` input semantics are identical in CLI, `GET ?as_of=` and the `POST` body:
  a bare date = 23:59:59 New York that day; naive datetimes = UTC.
- Duplicate reporting contexts: latest **eligible** filing wins; differing values are
  flagged `restated` with alternatives. Later comparatives/amendments are invisible to
  earlier `as_of` runs.
- FRED: observation date, vintage window, publication time (always unknown/null),
  `available_at` and `retrieved_at` are kept separate. Live `as_of` requests ALFRED for
  real-time date (as_of in ET) − 1 day.

### v0.2 event timing

| Source | `available_at` | Precision |
|---|---|---|
| SEC filings | EDGAR `acceptanceDateTime` (verified true UTC) | EXACT_TIMESTAMP |
| FOMC statements | RSS `pubDate`, else "For release at …" header | EXACT_TIMESTAMP |
| BLS/BEA releases | official scheduled time, date confirmed by ALFRED D−1 vs D vintages | SCHEDULED_TIME_CONFIRMED_DATE (strict mode: next-day) |
| metric-change events | v0.1 next-day policy | DATE_ONLY_CONSERVATIVE |
| calendars | none (not occurrences) | UNKNOWN |

Window starts given as bare dates mean 00:00 New York (`parse_window_start`); cutoffs mean
23:59:59 (`parse_as_of`). Rate limits: SEC 5/s, ALFRED 0.5/s (crawl-delay 2), Fed/BLS/BEA 1/s.

### v0.2 importance (`importance-v0.2.1`)

`score = 100 × Σ components / (100, +20 only if a consensus surprise exists)`; components
category 40 (half for revisions), magnitude 25 (0 for qualitative disclosures), extremeness 10,
novelty 10, relevance 15. Quantitative events without measurable magnitude are
INSUFFICIENT_DATA with no ranking score. Weights are judgmental, not trained.

## Accounting assumptions (summary — see methodology doc)

- Periodic forms only (10-K/10-Q/-T and /A). `fy`/`fp` describe the filing, not the fact;
  fiscal labels come from period end + fiscal-year-end month (52/53-week snapping).
- Standalone quarters: reported 3-month facts, or cumulative A − B only when same start
  date, B one step shorter, gap ~one quarter, unique match. Never sum quarters.
- Concepts chosen per period in priority order (filers change tags); concept recorded.
- CAPEX = cash purchases of PP&E (`PaymentsToAcquirePropertyPlantAndEquipment`; fallback
  `PaymentsToAcquireProductiveAssets`, flagged). Excludes finance leases, acquisitions,
  intangibles. FCF = OCF − CAPEX.
- Total debt (excl. leases) = `LongTermDebtNoncurrent + LongTermDebtCurrent` (carrying
  amounts) preferred; `LongTermDebt` only as flagged fallback (often face value);
  plus `CommercialPaper` else `ShortTermBorrowings`. `DebtCurrent` is never used
  (includes CP → double count). Net debt does not net marketable securities.
- Gross margin without `GrossProfit` falls back to cost of revenue (flagged).

## LLM behaviour and fallback

- Optional. Used for (a) ticker/metric extraction only when deterministic parsing fails
  (result validated against the SEC ticker map) and (b) narrative interpretation.
- Anthropic call: adaptive thinking, effort `medium`, JSON-schema structured output,
  server-side refusal fallbacks (`fallbacks="default"`, beta `server-side-fallback-2026-07-01`).
- Screening: every interpretation must cite existing evidence ids and every number must
  match cited evidence at displayed precision; unitless integers count as figures unless
  followed by a count noun ("8 quarters") or are years. The model's executive summary
  must contain no figures. Unsupported items are dropped and listed.
- No key, disabled, refusal, truncation, or API error → report still produced with a
  template-based factual summary; `llm.status` = `unavailable` | `failed`.

## Persistence notes

- Alembic head: `0006_event_studies` (v0.3-B event_study_runs/event_study_results; v0.3-A market tables in 0004; 0005 makes market versions unique by sequence; v0.2 tables: financial_events,
  event_sources, briefings, briefing_events — `docs/database_schema.md`). Always add a migration for model changes;
  `tests/integration/test_storage.py` fails if models and migrations diverge.
- Writes are idempotent and wrapped in `retry_on_conflict` (concurrent first loads);
  SQLite runs in WAL mode with a 30 s busy timeout.

## Testing conventions

- Offline by default; fixtures: `tests/fixtures/sec` = real SEC snapshot (trimmed),
  `tests/fixtures/fred` = **synthetic**, `tests/fixtures/{fed,bls,bea,alfred}` = **real** v0.2
  snapshots. `MANIFEST.json` records provenance and the v0.2 recording window.
- Synthetic event cases are constructed inline in tests and labelled SYNTHETIC.
- `tests/fixtures/eval/ranking_expectations.json` are regression expectations, not independent
  labels — never report ranking metrics from them.
- Financial arithmetic tests use hand-calculated values; replay tests must cover both
  included and excluded observations; add a regression test for every fixed defect.
- `tests/conftest.py` strips credential env vars; never rely on a developer's env.
- (v0.3-A) `tests/fixtures/alpaca` is **synthetic** (never call it provider data); never commit
  real provider responses (licensed). `tests/fixtures/alfred/DGS*` are **real**. The yield
  availability rule must stay "never earlier" on the recorded calibration vintages.
- CI (`.github/workflows/ci.yml`) must stay credential-free; live tests are opt-in only.
- (v0.3-B) Statistical tests need INDEPENDENT references (closed forms, explicit matrices,
  numpy `lstsq` on fixture files, scipy) — never a value produced by the same code path. Monte
  Carlo tests use fixed seeds. `study_ctx` (conftest) = REAL events + SYNTHETIC bars + REAL yields;
  synthetic test events must use `data_origin="fixture:synthetic"` and dates that do not collide
  with other tests in the module. A DB built with `create_all()` must be `alembic stamp head`-ed
  before the CLI uses it.

## Known limitations

Live FRED and live LLM synthesis unverified (no key / no API credit at freeze); only
us-gaap statement facts (no extensions, segments, MD&A text, prices); filing-level (not
passage-level) citations; current ticker map/company name used for historical runs;
"as originally reported" mode not implemented; synchronous research endpoint;
per-process rate limiting; PostgreSQL untested; numeric checker does not verify
direction words ("rose"/"fell").
v0.2: no consensus or market-price source (no surprises/reactions); judgmental importance
weights; macro intraday times from official schedules (dates confirmed); calendars not
versioned historically (briefings withhold calendar entries first observed after the cutoff —
`EventStore.schedule_known_at`); 8-K text not parsed (item metadata only); CORPORATE_GUIDANCE reserved
(no source); EDGAR dissemination lag not modelled.
v0.3-A: live Alpaca unverified (no credentials; offline prices synthetic); delisted coverage and
provider correction behaviour unverified; corporate actions carry no announcement date (known
from ex-date); halts indistinguishable from missing data; backfill assumes first-retrieved =
originally published; single-process SQLite (no demonstrated bottleneck at 12k bars).
v0.3-B: equity event studies are methodological validation only (synthetic prices; live Alpaca
unverified); classical single-event inference (normal i.i.d.) + placebo — no BMP/Kolari–Pynnönen/
rank tests or cross-asset clustered inference; daily data only; confounder coverage inferred from
stored events (no ingestion-window record); point-in-time studies over backfilled bars are
UNVERIFIED; stored report Markdown is immutable (rendering changes apply to new runs only).
