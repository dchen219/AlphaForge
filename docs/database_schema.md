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
