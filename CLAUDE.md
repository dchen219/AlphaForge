# CLAUDE.md — AlphaForge

AlphaForge: *an agentic financial intelligence and quantitative research platform.*
(Formerly "Finance Agent". The Python package `finance_agent`, distribution and CLI
command `finance-agent`, and database schema intentionally keep their original names —
do not rename them.)

Guidance for AI coding agents and contributors working in this repository.
Status: **v0.1 — Financial Research Foundation, frozen** on `master` (baseline audit 2026-11-02).
**v0.2 — Market Intelligence & Event Detection** implemented on branch
`feat/v0.2-market-intelligence` (in review; not merged). Roadmap: v0.3 Event-to-Asset Impact
Engine · v0.4 Portfolio Intelligence · v0.5 Quantitative Research Agents (not started — do not
start without an explicit request). Event semantics: `docs/event_intelligence_methodology.md`. Detailed history: `IMPLEMENTATION_LOG.md`. Accounting rules:
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

## Stack

Python 3.12 (uv) · FastAPI · Pydantic v2 / pydantic-settings · SQLAlchemy 2.x + Alembic ·
SQLite (PostgreSQL-compatible schema) · httpx · LangGraph (bounded linear workflow) ·
Anthropic SDK (`claude-opus-5-5`) · Typer/Rich · pytest · Ruff. `tzdata` is required on
Windows. pandas/NumPy are intentionally not used (scalar, auditable arithmetic).

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
tests/{unit,integration,historical_replay,fixtures}
scripts/build_fixtures.py       v0.1 fixtures (SEC real, FRED synthetic)
scripts/build_v02_fixtures.py   records REAL v0.2 fixtures by running ingestion live (~15 min)
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

- Alembic head: `0003_event_intelligence` (v0.2 tables: financial_events, event_sources,
  briefings, briefing_events — `docs/database_schema.md`). Always add a migration for model changes;
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

## Known limitations

Live FRED and live LLM synthesis unverified (no key / no API credit at freeze); only
us-gaap statement facts (no extensions, segments, MD&A text, prices); filing-level (not
passage-level) citations; current ticker map/company name used for historical runs;
"as originally reported" mode not implemented; synchronous research endpoint;
per-process rate limiting; PostgreSQL untested; numeric checker does not verify
direction words ("rose"/"fell").
v0.2: no consensus or market-price source (no surprises/reactions); judgmental importance
weights; macro intraday times from official schedules (dates confirmed); calendars not
versioned historically; 8-K text not parsed (item metadata only); CORPORATE_GUIDANCE reserved
(no source); EDGAR dissemination lag not modelled.
