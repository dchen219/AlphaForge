# AlphaForge

*An agentic financial intelligence and quantitative research platform.*

**Released: v0.1 — Financial Research Foundation. In review: v0.2 — Market Intelligence &
Event Detection** (branch `feat/v0.2-market-intelligence`). AlphaForge is a modular,
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
| v0.2 | Market Intelligence & Event Detection — macro/FOMC/SEC event ingestion, dedup, explainable importance, daily briefings | **Implemented — in review** |
| v0.3 | Event-to-Asset Impact Engine | Planned |
| v0.4 | Portfolio Intelligence | Planned |
| v0.5 | Quantitative Research Agents | Planned |

v0.3–v0.5 are not yet built. v0.2 deliberately stops short of prediction: no causal estimates,
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

# API
uv run finance-agent serve [--offline]    # http://127.0.0.1:8000/docs
```

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
```

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
| `fixture:real_snapshot` (v0.2) | `tests/fixtures/{fed,bls,bea,alfred}` and refreshed `sec/submissions`: **real** official responses recorded by `scripts/build_v02_fixtures.py` running the actual ingestion code (window 2025-07-01 → 2026-11-01) |

Reports and briefings print the origin in a banner. Example outputs: [docs/examples](docs/examples)
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
versioned historically; 8-K press-release text is not parsed (classification uses SEC item
metadata only); live LLM synthesis unverified (no API credit).
