"""Read-only system overview for the local testing dashboard (``GET /system/overview``).

Reports which data sources exist, whether the credentials each one needs are configured
(booleans only — never values), and what the local database already contains. Nothing here
fetches from a provider or writes to the database.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy import func, select

from finance_agent.config.logging import redact
from finance_agent.market.store import MarketStore
from finance_agent.storage.models import BriefingRecord, FinancialEventRecord, ResearchRun
from finance_agent.timeutil import ensure_aware

router = APIRouter(prefix="/system", tags=["system"])


def get_ctx(request: Request):
    return request.app.state.ctx


Ctx = Annotated[Any, Depends(get_ctx)]


class CredentialStatus(BaseModel):
    env_var: str
    configured: bool


class SourceStatus(BaseModel):
    key: str
    name: str
    used_for: str
    credentials: list[CredentialStatus]
    # Whether the live source can be used with the current configuration (offline ignores this).
    live_ready: bool
    live_note: str
    # What offline mode serves for this source (recorded in tests/fixtures/MANIFEST.json).
    offline_origin: str


class EventStoreSummary(BaseModel):
    source_name: str
    data_origins: list[str]
    events: int
    first_available_at: datetime | None
    last_available_at: datetime | None
    last_ingested_at: datetime | None


class MarketRunOut(BaseModel):
    run_id: str
    provider: str
    status: str
    params: dict
    stats: dict
    errors: list
    data_origin: str | None
    started_at: datetime
    finished_at: datetime | None


class SystemOverview(BaseModel):
    offline_mode: bool
    sources: list[SourceStatus]
    event_store: list[EventStoreSummary]
    # Event ingestion reports are returned by the CLI but not persisted; only store contents exist.
    event_ingestion_runs_persisted: bool = False
    market_instruments: int
    market_runs: list[MarketRunOut]
    market_quality_counts: dict[str, int]
    research_runs: dict[str, int]
    briefings: dict[str, int]
    http_ingestion_enabled: bool


def _set(v) -> bool:
    if v is None:
        return False
    return bool(v.get_secret_value() if hasattr(v, "get_secret_value") else v)


def _sources(s) -> list[SourceStatus]:
    sec = CredentialStatus(env_var="SEC_USER_AGENT", configured=_set(s.sec_user_agent))
    fred = CredentialStatus(env_var="FRED_API_KEY", configured=_set(s.fred_api_key))
    alpaca = [
        CredentialStatus(env_var="ALPACA_API_KEY_ID", configured=_set(s.alpaca_api_key_id)),
        CredentialStatus(env_var="ALPACA_API_SECRET_KEY", configured=_set(s.alpaca_api_secret_key)),
    ]
    anthropic = CredentialStatus(env_var="ANTHROPIC_API_KEY", configured=_set(s.anthropic_api_key))
    return [
        SourceStatus(
            key="sec",
            name="SEC EDGAR (companyfacts, submissions, ticker map)",
            used_for="company research, SEC filing events",
            credentials=[sec],
            live_ready=sec.configured,
            live_note="SEC requires a descriptive User-Agent with contact details.",
            offline_origin="fixture:real_snapshot",
        ),
        SourceStatus(
            key="fred",
            name="FRED/ALFRED macro series (keyed JSON API)",
            used_for="GET /macro/{series_id}",
            credentials=[fred],
            live_ready=fred.configured,
            live_note="Live FRED is unverified (no key at freeze).",
            offline_origin="fixture:synthetic",
        ),
        SourceStatus(
            key="official_events",
            name="Federal Reserve, BLS, BEA, ALFRED keyless CSV vintages",
            used_for="FOMC and macro release events, release calendars",
            credentials=[],
            live_ready=True,
            live_note="Keyless public sources.",
            offline_origin="fixture:real_snapshot",
        ),
        SourceStatus(
            key="treasury_yields",
            name="ALFRED H.15 Treasury yields (DGS2, DGS10)",
            used_for="market yields",
            credentials=[],
            live_ready=True,
            live_note="Keyless public source.",
            offline_origin="fixture:real_snapshot",
        ),
        SourceStatus(
            key="alpaca",
            name="Alpaca Market Data (daily bars, corporate actions)",
            used_for="market prices, returns, quality checks",
            credentials=alpaca,
            live_ready=all(c.configured for c in alpaca),
            live_note="Live Alpaca is unverified (no credentials at build time).",
            offline_origin="fixture:synthetic",
        ),
        SourceStatus(
            key="anthropic",
            name="Anthropic API (optional narrative interpretation)",
            used_for="LLM interpretation only — never a source of numbers",
            credentials=[anthropic],
            live_ready=anthropic.configured and s.llm_enabled,
            live_note="Without a key (or credit) reports use the deterministic template.",
            offline_origin="not used offline unless a key is configured",
        ),
    ]


def _by_status(db, column) -> dict[str, int]:
    with db.session() as sess:
        return {k: n for k, n in sess.execute(select(column, func.count()).group_by(column)).all()}


@router.get("/overview", response_model=SystemOverview)
def overview(ctx: Ctx) -> SystemOverview:
    E = FinancialEventRecord
    with ctx.db.session() as sess:
        rows = sess.execute(
            select(
                E.source_name,
                E.data_origin,
                func.count(),
                func.min(E.available_at),
                func.max(E.available_at),
                func.max(E.ingested_at),
            ).group_by(E.source_name, E.data_origin)
        ).all()
    merged: dict[str, dict] = {}
    for src, origin, n, lo, hi, ing in rows:
        m = merged.setdefault(src, {"origins": set(), "n": 0, "lo": [], "hi": [], "ing": []})
        m["origins"].add(origin)
        m["n"] += n
        m["lo"] += [lo] if lo else []
        m["hi"] += [hi] if hi else []
        m["ing"] += [ing] if ing else []

    def _agg(values: list, fn):
        return ensure_aware(fn(ensure_aware(v) for v in values)) if values else None

    store = MarketStore(ctx.db)
    token = ctx.settings.ingest_admin_token
    return SystemOverview(
        offline_mode=ctx.settings.offline_mode,
        sources=_sources(ctx.settings),
        event_store=[
            EventStoreSummary(
                source_name=src,
                data_origins=sorted(m["origins"]),
                events=m["n"],
                first_available_at=_agg(m["lo"], min),
                last_available_at=_agg(m["hi"], max),
                last_ingested_at=_agg(m["ing"], max),
            )
            for src, m in sorted(merged.items())
        ],
        market_instruments=len(store.instruments()),
        market_runs=[
            MarketRunOut(**{**r, "errors": [redact(str(e)) for e in r["errors"]]}) for r in store.runs(5)
        ],
        market_quality_counts=store.issue_counts(),
        research_runs=_by_status(ctx.db, ResearchRun.status),
        briefings=_by_status(ctx.db, BriefingRecord.status),
        http_ingestion_enabled=_set(token),
    )
