# Database Schema

SQLAlchemy 2 models in `src/finance_agent/storage/models.py`; Alembic migrations in
`src/finance_agent/storage/migrations/versions/` (never edit an applied migration — add a
new one). Portable types only (SQLite locally, PostgreSQL-compatible). All timestamps are
stored in UTC.

| Revision | Content |
|---|---|
| `09238e2d643f` (0001) | v0.1 schema |
| `0002_snapshot_last_retrieved` | `source_snapshots.last_retrieved_at` (cache freshness) |
| `0003_event_intelligence` | v0.2 event tables |
| `0004_market_data` | v0.3-A market-data tables |
| `0005_market_version_identity` | v0.3-A: market bar / corporate-action / yield versions unique by (key, `version`) instead of (key, `content_hash`), so a source reverting to an earlier value is recorded |
| `0006_event_studies` | v0.3-B event-study runs (immutable, content-addressed) and per-window results |

## v0.1 tables

| Table | Purpose / key columns |
|---|---|
| `companies` | `cik` (PK), name, fiscal_year_end, SIC |
| `securities` | ticker (unique) → cik |
| `filings` | `accession` (PK), form, filing_date, report_date, acceptance_datetime (raw), primary_document |
| `source_documents` | logical source (`provider`, `source_key` unique), redacted URL |
| `source_snapshots` | immutable response body on disk addressed by `sha256` (unique per document); retrieved_at (first seen), last_retrieved_at, origin |
| `financial_facts` | raw XBRL facts (`fact_hash` unique): concept, unit, period, value, accession, form, filed |
| `macro_series`, `macro_observations` | FRED/ALFRED metadata and vintage rows (unique series/date/realtime_start) |
| `research_runs`, `research_evidence`, `research_reports` | v0.1 company research workflow |

## v0.2 tables

### `financial_events`
One row per normalized event. `event_id` = `"EV-" + sha256(canonical_event_key)[:16]`.

| Column group | Columns |
|---|---|
| identity | `event_id` (PK), `canonical_event_key` (UNIQUE), `event_type`, `event_subtype`, `event_status`, `revision_status` |
| description | `title`, `description`, `attributes` (JSON: e.g. items, target range, vintage dates) |
| source | `source_name`, `source_url`, `source_document_id` (accession / statement id / vintage id), `source_snapshot_hash`, `data_origin` |
| timing | `occurred_at`, `scheduled_for`, `published_at`, `available_at` (indexed), `ingested_at`, `availability_precision` |
| observation | `series_id`, `observation_period`, `observation_date`, `value`, `previous_value`, `unit`, `consensus_value`, `consensus_status` |
| intelligence | `importance_score`, `importance_level`, `importance_components` (JSON), `comparisons` (JSON), `channels` (JSON), `verification_status` |
| links | `related_event_id`, `supersedes_event_id`, `event_cluster_id` (indexed) |
| audit | `created_at`, `updated_at` |

Observed columns (values, times, status, period, source document) are immutable after
insert; revisions are new rows that supersede earlier ones.

### `event_sources`
Evidence for an event; several sources may corroborate one event.
`(event_id, source_key)` UNIQUE · `role` (primary | corroborating | schedule | vintage |
vintage_prior) · `source_url` · `snapshot_id` → `source_snapshots` · `snapshot_sha256` ·
`retrieved_at` · `excerpt` (verbatim passage, e.g. the FOMC decision sentence).

### `briefings`
`briefing_id` (PK) · `as_of`, `window_start`, `window_end` · `status` · `verification_status`
· `params`, `steps` (JSON) · `report_json` · `markdown` · `error` · timestamps.

### `briefing_events`
`(briefing_id, event_id)` UNIQUE · `section` (macro | corporate) · `rank` · `importance_score`.

## v0.3-A market-data tables

Raw provider responses stay in `source_snapshots` (immutable). Normalized observations are
versioned: a new row per distinct observation, never an overwrite.

| Table | Key columns |
|---|---|
| `market_instruments` | `instrument_id` (PK), name, asset_class, group_name, currency, primary_exchange, calendar, active |
| `market_provider_mappings` | instrument → (provider, provider_symbol), valid_from/valid_to (symbol changes); UNIQUE(instrument, provider, symbol) |
| `market_price_bars` | instrument, provider, provider_symbol, exchange, frequency, `adjustment` (raw / provider:split / provider:all), session_date, bar_timestamp, timezone, OHLCV, vwap, trade_count, currency, `content_hash`, `version`, snapshot_id/sha256, data_origin, retrieved_at, `available_at` (indexed), availability_precision, `bar_status` (FINAL/PROVISIONAL), `quality_status` (OK/WARNING/QUARANTINED). UNIQUE(instrument, provider, frequency, adjustment, session_date, version); index (instrument, adjustment, session_date) |
| `market_corporate_actions` | provider `action_id`, instrument, action_type (STOCK_SPLIT/CASH_DIVIDEND/SYMBOL_CHANGE/MERGER/SPINOFF), ex/record/payable/process/declaration dates, old_rate/new_rate, cash_amount, currency, new_symbol, content_hash, version, snapshot, retrieved_at, available_at, precision, raw JSON. UNIQUE(provider, action_id, version) |
| `market_trading_sessions` | (calendar, session_date) PK; market_open/close, pre_market_open, after_hours_close (UTC), early_close |
| `market_quality_issues` | `fingerprint` UNIQUE (idempotent), check, severity, instrument, provider, session_date, detail, status (OPEN/QUARANTINED/EXPLAINED), run_id |
| `market_ingestion_runs` | run_id, provider, params, status, stats (rows, versions, unchanged, duplicates, corrections, cache hits, requests, duration), errors, data_origin |
| `market_yield_observations` | series_id, observation_date, value (percent; null = missing), units, vintage_date, first_vintage_rule, content_hash, version, available_at, precision, snapshot, data_origin, retrieved_at. UNIQUE(series, date, version) |

Adjusted prices (split-adjusted, total-return) are **derived at query time** from raw bars and the
corporate actions visible at the cutoff. They are not stored, so a point-in-time query can never
pick up a future action.

## v0.3-B tables (migration `0006_event_studies`)

| Table | Purpose / key columns |
|---|---|
| `event_study_runs` | `run_id` PK = `ES-` + sha256(kind, spec_hash, data_digest, methodology)[:16]; kind (single/yield/compare), status, `spec_hash` (indexed), `data_digest`, methodology_version, specification JSON, event_ids, data_mode, data_origin, `synthetic`, software versions, data_manifest (bar versions, snapshot SHA-256s, corrected sessions, factors), result JSON, report_markdown, created_at, finalized_at. Rows are never updated: identical inputs reuse the run, changed inputs create a new one. |
| `event_study_results` | id, `run_id` FK (indexed), event_id (or `AGGREGATE`), target, variant (`primary` or robustness label), model, window, window_start/end, status, measure (`car` / `raw_cumulative_return` / `change_bp` / `mean_cluster_car`), unit (`decimal_return` / `basis_points`), value, standard_error, test_statistic, p_value, ci_low, ci_high, inference_status, estimation_observations. UNIQUE(run_id, event_id, variant, window, measure) |
