"""Application service layer shared by the API, CLI and workflow."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from finance_agent.agents.llm import LLMProvider, build_llm
from finance_agent.config.settings import Settings
from finance_agent.errors import ConfigurationError
from finance_agent.http import FixtureTransport, HttpClient
from finance_agent.providers.base import CachedFetcher
from finance_agent.providers.fred import FredProvider
from finance_agent.providers.sec import SecProvider, normalize_ticker
from finance_agent.research.calculations import compute_annual_metrics, compute_quarterly_metrics
from finance_agent.research.normalize import FiscalCalendar, build_quarterly_financials
from finance_agent.schemas.financials import CalculationResult, QuarterlyFinancials, SnapshotInfo
from finance_agent.schemas.macro import MacroObservationOut, MacroSeriesResponse
from finance_agent.schemas.sec import CompanyProfile, FilingMeta, XbrlFact
from finance_agent.storage.db import Database
from finance_agent.storage.repository import Repository
from finance_agent.storage.snapshots import SnapshotRef, SnapshotStore
from finance_agent.timeutil import AVAILABILITY_POLICY, available_at

log = logging.getLogger(__name__)


class CompanyOverview(BaseModel):
    ticker: str
    profile: CompanyProfile
    recent_filings: list[FilingMeta]
    data_origin: str
    snapshot: SnapshotInfo


class FinancialsResponse(BaseModel):
    financials: QuarterlyFinancials
    calculations: list[CalculationResult]
    annual_calculations: list[CalculationResult]


class CompanyData(BaseModel):
    ticker: str
    profile: CompanyProfile
    filings: list[FilingMeta]
    facts: list[XbrlFact]
    snapshots: list[SnapshotInfo]


def _info(ref: SnapshotRef) -> SnapshotInfo:
    return SnapshotInfo(sha256=ref.sha256, url=ref.url, retrieved_at=ref.retrieved_at, origin=ref.origin)


def combined_origin(snaps: list[SnapshotInfo]) -> str:
    origins = sorted({s.origin for s in snaps})
    return origins[0] if len(origins) == 1 else "mixed:" + ",".join(origins)


class AppContext:
    def __init__(
        self,
        settings: Settings,
        *,
        db: Database | None = None,
        transport=None,
        llm: LLMProvider | None = None,
        migrate: bool = True,
    ) -> None:
        self.settings = settings
        self.db = db or Database(settings.database_url)
        if db is None and migrate:
            self.db.migrate()
        if settings.offline_mode and transport is None:
            transport = FixtureTransport(settings.fixtures_dir)
        ua = settings.sec_user_agent or "FinanceAgent/0.1 (SEC_USER_AGENT not configured)"
        self.http = HttpClient(
            user_agent=ua,
            timeout=settings.http_timeout_seconds,
            max_attempts=settings.http_max_attempts,
            transport=transport,
            rate_limits={
                "www.sec.gov": settings.sec_max_requests_per_second,
                "data.sec.gov": settings.sec_max_requests_per_second,
                "api.stlouisfed.org": settings.fred_max_requests_per_second,
                "alfred.stlouisfed.org": settings.alfred_max_requests_per_second,
                "www.federalreserve.gov": settings.official_max_requests_per_second,
                "www.bls.gov": settings.official_max_requests_per_second,
                "www.bea.gov": settings.official_max_requests_per_second,
            },
        )
        self.snapshots = SnapshotStore(self.db, settings.snapshots_dir)
        self.repo = Repository(self.db)
        self.sec = SecProvider(
            CachedFetcher("sec", self.http, self.snapshots),
            ttl_tickers=timedelta(hours=settings.cache_ttl_tickers_hours),
            ttl_sec=timedelta(hours=settings.cache_ttl_sec_hours),
        )
        fred_key = settings.fred_api_key.get_secret_value() if settings.fred_api_key else None
        self.fred = FredProvider(
            CachedFetcher("fred", self.http, self.snapshots),
            fred_key,
            ttl=timedelta(hours=settings.cache_ttl_fred_hours),
        )
        anthropic_key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
        self.llm = llm or build_llm(anthropic_key, settings.anthropic_model, settings.llm_enabled)

    def close(self) -> None:
        self.http.close()

    # ------------------------------------------------------------ SEC
    def _require_sec_identity(self) -> None:
        if not self.http.is_fixture and not self.settings.sec_user_agent:
            raise ConfigurationError(
                "SEC_USER_AGENT must be set for live SEC access (SEC fair-access policy requires a "
                "descriptive User-Agent with contact information)."
            )

    def company(self, ticker: str) -> CompanyOverview:
        self._require_sec_identity()
        entry = self.sec.resolve_ticker(ticker)
        profile, filings, ref = self.sec.submissions(entry.cik)
        self.repo.upsert_company(profile, entry.ticker)
        self.repo.upsert_filings(filings, ref.snapshot_id)
        return CompanyOverview(
            ticker=entry.ticker,
            profile=profile,
            recent_filings=filings[:20],
            data_origin=ref.origin,
            snapshot=_info(ref),
        )

    def company_data(self, ticker: str) -> CompanyData:
        self._require_sec_identity()
        entry = self.sec.resolve_ticker(ticker)
        profile, filings, sub_ref = self.sec.submissions(entry.cik)
        self.repo.upsert_company(profile, entry.ticker)
        self.repo.upsert_filings(filings, sub_ref.snapshot_id)
        parsed, facts_ref = self.sec.companyfacts(entry.cik)
        inserted = self.repo.insert_facts(parsed.facts, facts_ref.snapshot_id)
        log.info("facts stored", extra={"cik": entry.cik, "parsed": len(parsed.facts), "new": inserted})
        return CompanyData(
            ticker=entry.ticker,
            profile=profile,
            filings=filings,
            facts=parsed.facts,
            snapshots=[_info(sub_ref), _info(facts_ref)],
        )

    def build_financials(
        self, data: CompanyData, quarters: int, as_of: datetime | None
    ) -> QuarterlyFinancials:
        cal = FiscalCalendar.from_mmdd(data.profile.fiscal_year_end) or FiscalCalendar.infer(data.facts)
        fin = build_quarterly_financials(
            data.facts,
            cik=data.profile.cik,
            company_name=data.profile.name,
            ticker=data.ticker,
            calendar=cal,
            as_of=as_of,
            quarters=quarters,
        )
        fin.snapshots = data.snapshots
        fin.data_origin = combined_origin(data.snapshots)
        return fin

    def financials(self, ticker: str, quarters: int = 8, as_of: datetime | None = None) -> FinancialsResponse:
        fin = self.build_financials(self.company_data(ticker), quarters, as_of)
        return FinancialsResponse(
            financials=fin,
            calculations=compute_quarterly_metrics(fin),
            annual_calculations=compute_annual_metrics(fin),
        )

    # ------------------------------------------------------------ FRED
    def macro(
        self,
        series_id: str,
        *,
        as_of: datetime | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> MacroSeriesResponse:
        meta, meta_ref = self.fred.series_meta(series_id)
        rows, ref, vintage = self.fred.observations(
            series_id, as_of=as_of, observation_start=start, observation_end=end
        )
        self.repo.upsert_macro(meta, rows, ref.snapshot_id, meta_ref.snapshot_id, ref.retrieved_at)
        warnings = []
        if any(r.value is None for r in rows):
            warnings.append("Some observations are missing in the source ('.'); reported as null.")
        if as_of is not None:
            warnings.append(
                "Vintage selected for as_of under the next-day availability policy; "
                "intraday release times are unknown."
            )
        obs = [
            MacroObservationOut(
                series_id=meta.series_id,
                observation_date=r.observation_date,
                value=r.value,
                missing=r.value is None,
                realtime_start=r.realtime_start,
                realtime_end=r.realtime_end,
                vintage_date=r.realtime_start,
                available_at=available_at(r.realtime_start),
                retrieved_at=ref.retrieved_at,
                source_url=ref.url,
                snapshot_sha256=ref.sha256,
                data_origin=ref.origin,
            )
            for r in rows
        ]
        return MacroSeriesResponse(
            series=meta,
            as_of=as_of,
            vintage_policy=AVAILABILITY_POLICY,
            effective_vintage_date=vintage,
            observations=obs,
            data_origin=ref.origin,
            warnings=warnings,
        )

    # ------------------------------------------------------------ helpers
    @staticmethod
    def normalize_ticker(ticker: str) -> str:
        return normalize_ticker(ticker)
