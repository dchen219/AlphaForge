"""SQLAlchemy 2.x ORM models.

Design notes
- Portable types only (String/Text/Date/DateTime/Float/JSON) so the schema runs on
  SQLite locally and on PostgreSQL unchanged.
- ``SourceSnapshot`` rows are immutable: content lives on disk addressed by its
  SHA-256 and the hash is re-verified on every read.
- ``FinancialFact`` stores raw XBRL facts (one row per accession × concept × unit ×
  period); normalized quarterly values are derived deterministically from these
  rows and carry full provenance in their output schema.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from finance_agent.timeutil import utcnow


class Base(DeclarativeBase):
    pass


class Company(Base):
    __tablename__ = "companies"

    cik: Mapped[str] = mapped_column(String(10), primary_key=True)  # zero-padded
    name: Mapped[str] = mapped_column(String(255))
    fiscal_year_end: Mapped[str | None] = mapped_column(String(4))  # MMDD
    sic: Mapped[str | None] = mapped_column(String(10))
    sic_description: Mapped[str | None] = mapped_column(String(255))
    entity_type: Mapped[str | None] = mapped_column(String(50))
    state_of_incorporation: Mapped[str | None] = mapped_column(String(10))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    securities: Mapped[list[Security]] = relationship(back_populates="company")


class Security(Base):
    __tablename__ = "securities"
    __table_args__ = (UniqueConstraint("ticker", name="uq_security_ticker"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticker: Mapped[str] = mapped_column(String(16))
    cik: Mapped[str] = mapped_column(ForeignKey("companies.cik"))
    exchange: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str | None] = mapped_column(String(255))

    company: Mapped[Company] = relationship(back_populates="securities")


class Filing(Base):
    __tablename__ = "filings"

    accession: Mapped[str] = mapped_column(String(20), primary_key=True)
    cik: Mapped[str] = mapped_column(ForeignKey("companies.cik"), index=True)
    form: Mapped[str] = mapped_column(String(20))
    filing_date: Mapped[date] = mapped_column(Date)
    report_date: Mapped[date | None] = mapped_column(Date)
    acceptance_datetime: Mapped[str | None] = mapped_column(String(32))  # raw, as published
    primary_document: Mapped[str | None] = mapped_column(String(255))
    is_amendment: Mapped[bool] = mapped_column(Boolean, default=False)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshots.id"))


class SourceDocument(Base):
    """A logical source, e.g. 'SEC companyfacts for CIK0000789019'."""

    __tablename__ = "source_documents"
    __table_args__ = (UniqueConstraint("provider", "source_key", name="uq_source_document"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32))  # sec | fred
    source_key: Mapped[str] = mapped_column(String(255))  # stable cache key
    url: Mapped[str] = mapped_column(Text)  # credential-redacted
    kind: Mapped[str] = mapped_column(String(64))
    description: Mapped[str | None] = mapped_column(Text)

    snapshots: Mapped[list[SourceSnapshot]] = relationship(back_populates="document")


class SourceSnapshot(Base):
    """Immutable captured response body (stored on disk, addressed by SHA-256)."""

    __tablename__ = "source_snapshots"
    __table_args__ = (UniqueConstraint("source_document_id", "sha256", name="uq_snapshot_doc_hash"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_document_id: Mapped[int] = mapped_column(ForeignKey("source_documents.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    storage_path: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(Integer)
    http_status: Mapped[int] = mapped_column(Integer)
    content_type: Mapped[str | None] = mapped_column(String(128))
    origin: Mapped[str] = mapped_column(String(64))  # live | fixture:real_snapshot | fixture:synthetic
    # Retrieval metadata (content stays immutable): last time identical content was fetched.
    last_retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    document: Mapped[SourceDocument] = relationship(back_populates="snapshots")


class FinancialFact(Base):
    __tablename__ = "financial_facts"
    __table_args__ = (
        UniqueConstraint("fact_hash", name="uq_fact_hash"),
        Index("ix_fact_lookup", "cik", "concept", "period_end"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    fact_hash: Mapped[str] = mapped_column(String(64))
    cik: Mapped[str] = mapped_column(ForeignKey("companies.cik"))
    taxonomy: Mapped[str] = mapped_column(String(32))
    concept: Mapped[str] = mapped_column(String(255))
    unit: Mapped[str] = mapped_column(String(32))
    period_start: Mapped[date | None] = mapped_column(Date)  # None => instant
    period_end: Mapped[date] = mapped_column(Date)
    value: Mapped[float] = mapped_column(Float)
    accession: Mapped[str] = mapped_column(String(20), index=True)
    form: Mapped[str] = mapped_column(String(20))
    filed: Mapped[date] = mapped_column(Date)
    filer_fiscal_year: Mapped[int | None] = mapped_column(Integer)  # 'fy' of the FILING
    filer_fiscal_period: Mapped[str | None] = mapped_column(String(4))  # 'fp' of the FILING
    frame: Mapped[str | None] = mapped_column(String(16))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("source_snapshots.id"))


class MacroSeries(Base):
    __tablename__ = "macro_series"

    series_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(Text)
    units: Mapped[str | None] = mapped_column(String(255))
    frequency: Mapped[str | None] = mapped_column(String(64))
    seasonal_adjustment: Mapped[str | None] = mapped_column(String(128))
    last_updated: Mapped[str | None] = mapped_column(String(64))  # as published by FRED
    notes: Mapped[str | None] = mapped_column(Text)
    snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("source_snapshots.id"))


class MacroObservation(Base):
    __tablename__ = "macro_observations"
    __table_args__ = (
        UniqueConstraint("series_id", "observation_date", "realtime_start", name="uq_macro_obs_vintage"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_id: Mapped[str] = mapped_column(String(64), index=True)
    observation_date: Mapped[date] = mapped_column(Date)  # the period measured
    value: Mapped[float | None] = mapped_column(Float)  # None => missing ('.')
    realtime_start: Mapped[date] = mapped_column(Date)  # vintage validity start
    realtime_end: Mapped[date] = mapped_column(Date)  # vintage validity end
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    snapshot_id: Mapped[int] = mapped_column(ForeignKey("source_snapshots.id"))


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    question: Mapped[str] = mapped_column(Text)
    ticker: Mapped[str | None] = mapped_column(String(16))
    cik: Mapped[str | None] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(32))  # running | completed | failed
    as_of: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    steps: Mapped[list] = mapped_column(JSON, default=list)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchEvidence(Base):
    __tablename__ = "research_evidence"
    __table_args__ = (UniqueConstraint("run_id", "evidence_id", name="uq_run_evidence"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), index=True)
    evidence_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))  # fact | calculation | filing | trend
    payload: Mapped[dict] = mapped_column(JSON)


class ResearchReport(Base):
    __tablename__ = "research_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("research_runs.id"), unique=True)
    report_json: Mapped[dict] = mapped_column(JSON)
    markdown: Mapped[str] = mapped_column(Text)
    verification_status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
