"""Migrations, snapshot integrity/dedup, caching and source-metadata preservation."""

from __future__ import annotations

import gzip
from datetime import timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select

from finance_agent.errors import IntegrityError
from finance_agent.http import FetchResult
from finance_agent.services import AppContext
from finance_agent.storage.db import Database
from finance_agent.storage.models import (
    Base,
    Company,
    Filing,
    FinancialFact,
    MacroObservation,
    Security,
    SourceSnapshot,
)
from finance_agent.timeutil import utcnow
from tests.conftest import offline_settings


def test_alembic_migration_creates_schema_matching_models(tmp_path):
    db = Database(f"sqlite:///{(tmp_path / 'm.db').as_posix()}")
    db.migrate()
    db.migrate()  # idempotent
    tables = set(inspect(db.engine).get_table_names())
    assert set(Base.metadata.tables) <= tables and "alembic_version" in tables
    with db.engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], diff


def test_snapshot_dedup_and_integrity_check(ctx):
    res = FetchResult(
        url="https://example.test/a.json",
        status_code=200,
        content=b'{"a": 1}',
        retrieved_at=utcnow(),
        origin="live",
    )
    r1 = ctx.snapshots.save(provider="sec", source_key="k", kind="t", result=res)
    r2 = ctx.snapshots.save(provider="sec", source_key="k", kind="t", result=res)
    assert r1.snapshot_id == r2.snapshot_id
    with ctx.db.session() as s:
        snap = s.get(SourceSnapshot, r1.snapshot_id)
        path = snap.storage_path
    assert ctx.snapshots.read_blob(r1.sha256, path) == b'{"a": 1}'
    with gzip.open(path, "wb") as fh:  # tamper with stored content
        fh.write(b'{"a": 2}')
    with pytest.raises(IntegrityError):
        ctx.snapshots.read_blob(r1.sha256, path)


def test_fresh_snapshot_is_reused_instead_of_refetching(ctx):
    ctx.sec.submissions("0000789019")
    n = ctx.http.request_count
    _, _, ref = ctx.sec.submissions("0000789019")
    assert ctx.http.request_count == n and ref.from_cache
    assert ctx.snapshots.latest_fresh("sec", "submissions:0000789019", timedelta(0), "fixture") is None


def test_company_facts_and_filings_persist_full_provenance(ctx):
    data = ctx.company_data("MSFT")
    with ctx.db.session() as s:
        assert s.get(Company, "0000789019").fiscal_year_end == "0630"
        assert s.scalar(select(Security).where(Security.ticker == "MSFT")).cik == "0000789019"
        n_facts = s.query(FinancialFact).filter_by(cik="0000789019").count()
        f = s.scalars(select(FinancialFact).where(FinancialFact.cik == "0000789019")).first()
        snap = s.get(SourceSnapshot, f.snapshot_id)
        assert s.query(Filing).filter_by(cik="0000789019").count() > 0
    assert n_facts == len(data.facts)
    assert f.accession and f.filed and f.unit and f.concept and f.period_end
    assert snap.origin == "fixture:real_snapshot" and len(snap.sha256) == 64
    # Re-loading does not duplicate (content-hash dedup) and round-trips exactly.
    ctx.company_data("MSFT")
    assert len(ctx.repo.load_facts("0000789019")) == n_facts
    assert set(ctx.repo.load_facts("0000789019")) == set(data.facts)


def test_macro_observations_persist_vintages(ctx):
    ctx.macro("UNRATE")
    with ctx.db.session() as s:
        rows = s.scalars(select(MacroObservation).where(MacroObservation.series_id == "UNRATE")).all()
        assert rows and all(r.realtime_start and r.realtime_end and r.retrieved_at for r in rows)


def test_app_context_migrates_real_database(tmp_path):
    c = AppContext(offline_settings(tmp_path))
    assert c.db.ping()
    c.close()
