"""Persistence operations (thin, explicit, idempotent)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from finance_agent.providers.fred import VintageRow
from finance_agent.schemas.macro import MacroSeriesMeta
from finance_agent.schemas.sec import CompanyProfile, FilingMeta, XbrlFact
from finance_agent.storage.db import Database, retry_on_conflict
from finance_agent.storage.models import (
    Company,
    Filing,
    FinancialFact,
    MacroObservation,
    MacroSeries,
    ResearchEvidence,
    ResearchReport,
    ResearchRun,
    Security,
)
from finance_agent.timeutil import utcnow


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -------------------------------------------------------------- companies
    @retry_on_conflict
    def upsert_company(self, profile: CompanyProfile, ticker: str | None = None) -> None:
        with self.db.session() as s:
            c = s.get(Company, profile.cik) or Company(cik=profile.cik, name=profile.name)
            c.name = profile.name
            c.fiscal_year_end = profile.fiscal_year_end
            c.sic = profile.sic
            c.sic_description = profile.sic_description
            c.entity_type = profile.entity_type
            c.state_of_incorporation = profile.state_of_incorporation
            c.updated_at = utcnow()
            s.merge(c)
            s.flush()
            tickers = set(profile.tickers) | ({ticker} if ticker else set())
            for i, t in enumerate(sorted(tickers)):
                sec = s.scalar(select(Security).where(Security.ticker == t))
                if sec is None:
                    exch = profile.exchanges[i] if i < len(profile.exchanges) else None
                    s.add(Security(ticker=t, cik=profile.cik, exchange=exch, title=profile.name))
                else:
                    sec.cik = profile.cik

    @retry_on_conflict
    def upsert_filings(self, filings: list[FilingMeta], snapshot_id: int | None) -> int:
        with self.db.session() as s:
            existing = set(
                s.scalars(
                    select(Filing.accession).where(Filing.accession.in_([f.accession for f in filings]))
                )
            )
            new = 0
            for f in filings:
                if f.accession in existing:
                    continue
                s.add(
                    Filing(
                        accession=f.accession,
                        cik=f.cik,
                        form=f.form,
                        filing_date=f.filing_date,
                        report_date=f.report_date,
                        acceptance_datetime=f.acceptance_datetime,
                        primary_document=f.primary_document,
                        is_amendment=f.is_amendment,
                        snapshot_id=snapshot_id,
                    )
                )
                new += 1
            return new

    # -------------------------------------------------------------- facts
    @retry_on_conflict
    def insert_facts(self, facts: list[XbrlFact], snapshot_id: int) -> int:
        if not facts:
            return 0
        cik = facts[0].cik
        with self.db.session() as s:
            existing = set(s.scalars(select(FinancialFact.fact_hash).where(FinancialFact.cik == cik)))
            rows = [
                FinancialFact(
                    fact_hash=f.fact_hash,
                    cik=f.cik,
                    taxonomy=f.taxonomy,
                    concept=f.concept,
                    unit=f.unit,
                    period_start=f.start,
                    period_end=f.end,
                    value=f.value,
                    accession=f.accession,
                    form=f.form,
                    filed=f.filed,
                    filer_fiscal_year=f.fy,
                    filer_fiscal_period=f.fp,
                    frame=f.frame,
                    snapshot_id=snapshot_id,
                )
                for f in facts
                if f.fact_hash not in existing
            ]
            s.add_all(rows)
            return len(rows)

    def load_facts(self, cik: str) -> list[XbrlFact]:
        with self.db.session() as s:
            rows = s.scalars(select(FinancialFact).where(FinancialFact.cik == cik)).all()
            return [
                XbrlFact(
                    cik=r.cik,
                    taxonomy=r.taxonomy,
                    concept=r.concept,
                    unit=r.unit,
                    start=r.period_start,
                    end=r.period_end,
                    value=r.value,
                    accession=r.accession,
                    form=r.form,
                    filed=r.filed,
                    fy=r.filer_fiscal_year,
                    fp=r.filer_fiscal_period,
                    frame=r.frame,
                )
                for r in rows
            ]

    # -------------------------------------------------------------- macro
    @retry_on_conflict
    def upsert_macro(
        self,
        meta: MacroSeriesMeta,
        rows: list[VintageRow],
        snapshot_id: int,
        meta_snapshot_id: int | None,
        retrieved_at: datetime,
    ) -> int:
        with self.db.session() as s:
            ms = s.get(MacroSeries, meta.series_id) or MacroSeries(series_id=meta.series_id, title=meta.title)
            for k, v in meta.model_dump().items():
                setattr(ms, k, v)
            ms.snapshot_id = meta_snapshot_id
            s.merge(ms)
            existing = set(
                s.execute(
                    select(MacroObservation.observation_date, MacroObservation.realtime_start).where(
                        MacroObservation.series_id == meta.series_id
                    )
                ).all()
            )
            new = 0
            for r in rows:
                if (r.observation_date, r.realtime_start) in existing:
                    continue
                s.add(
                    MacroObservation(
                        series_id=meta.series_id,
                        observation_date=r.observation_date,
                        value=r.value,
                        realtime_start=r.realtime_start,
                        realtime_end=r.realtime_end,
                        retrieved_at=retrieved_at,
                        snapshot_id=snapshot_id,
                    )
                )
                new += 1
            return new

    # -------------------------------------------------------------- research
    def create_run(self, run_id: str, question: str, params: dict[str, Any], as_of: datetime | None) -> None:
        with self.db.session() as s:
            s.add(
                ResearchRun(
                    id=run_id, question=question, status="running", params=params, as_of=as_of, steps=[]
                )
            )

    def update_run(self, run_id: str, **fields: Any) -> None:
        with self.db.session() as s:
            run = s.get(ResearchRun, run_id)
            if run is None:
                return
            for k, v in fields.items():
                setattr(run, k, v)

    def save_report(
        self,
        run_id: str,
        report_json: dict,
        markdown: str,
        verification_status: str,
        evidence: list[tuple[str, str, dict]],
    ) -> None:
        with self.db.session() as s:
            for eid, kind, payload in evidence:
                s.add(ResearchEvidence(run_id=run_id, evidence_id=eid, kind=kind, payload=payload))
            s.add(
                ResearchReport(
                    run_id=run_id,
                    report_json=report_json,
                    markdown=markdown,
                    verification_status=verification_status,
                )
            )

    def get_run(self, run_id: str) -> tuple[ResearchRun, ResearchReport | None] | None:
        with self.db.session() as s:
            run = s.get(ResearchRun, run_id)
            if run is None:
                return None
            rep = s.scalar(select(ResearchReport).where(ResearchReport.run_id == run_id))
            s.expunge_all()
            return run, rep

    def evidence_for_run(self, run_id: str) -> list[ResearchEvidence]:
        with self.db.session() as s:
            rows = s.scalars(select(ResearchEvidence).where(ResearchEvidence.run_id == run_id)).all()
            s.expunge_all()
            return list(rows)
