# AlphaForge

*An agentic financial intelligence and quantitative research platform.*

**Current release: v0.1 — Financial Research Foundation.** AlphaForge is a modular,
local-first financial research platform. It retrieves official SEC EDGAR filings
and FRED/ALFRED macro data, normalizes them with full provenance, runs deterministic
fundamental calculations, and produces evidence-linked company research reports via a
FastAPI service and a CLI.

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
| v0.2 | Market Intelligence & Event Detection | Planned |
| v0.3 | Event-to-Asset Impact Engine | Planned |
| v0.4 | Portfolio Intelligence | Planned |
| v0.5 | Quantitative Research Agents | Planned |

Only v0.1 is implemented; later milestones are not yet built.

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

# API
uv run finance-agent serve [--offline]    # http://127.0.0.1:8000/docs
```

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

## API

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Status, DB check, which credentials are configured (booleans only) |
| GET | `/companies/{ticker}` | SEC profile and recent 10-K/10-Q filings |
| GET | `/companies/{ticker}/financials?quarters=8&as_of=` | Normalized quarterly values + calculations, with provenance |
| GET | `/macro/{series_id}?as_of=&start=&end=` | FRED/ALFRED observations with vintage metadata |
| POST | `/research/company` | `{"ticker"?, "question"?, "quarters": 8, "as_of"?, "use_llm": true}` → run + report |
| GET | `/research/{run_id}` | Stored run, steps, and report |

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
```

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

Reports print the origin in a banner. Example outputs: [docs/examples](docs/examples)
(`MSFT_live_report.md` was generated from live SEC data).

## Limitations (v0.1)

See IMPLEMENTATION_LOG.md → "Limitations" and CLAUDE.md for the full list. Highlights: live FRED and
live LLM synthesis were not verifiable in the build environment; only XBRL financial
statement data is analysed (no MD&A text, segments, guidance or prices); custom
company-specific XBRL extension concepts are ignored; research requests run synchronously.
