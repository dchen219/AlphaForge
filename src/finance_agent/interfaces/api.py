"""FastAPI HTTP interface."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from finance_agent import __version__
from finance_agent.config.logging import configure_logging, redact
from finance_agent.config.settings import Settings, get_settings
from finance_agent.errors import (
    ConfigurationError,
    DataUnavailableError,
    FinanceAgentError,
    MalformedDataError,
    NotFoundError,
    ProviderError,
)
from finance_agent.schemas.macro import MacroSeriesResponse
from finance_agent.schemas.research import ResearchReportOut, ResearchRequest, ResearchRunOut
from finance_agent.services import AppContext, CompanyOverview, FinancialsResponse
from finance_agent.timeutil import AVAILABILITY_POLICY, parse_as_of, utcnow
from finance_agent.workflows.company_research import CompanyResearchWorkflow

log = logging.getLogger(__name__)

Ticker = Annotated[str, Path(pattern=r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$", description="Stock ticker")]
SeriesId = Annotated[str, Path(pattern=r"^[A-Za-z0-9_]{1,30}$", description="FRED series id")]


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


Ctx = Annotated[AppContext, Depends(get_ctx)]


class HealthResponse(BaseModel):
    status: str
    version: str
    time: datetime
    offline_mode: bool
    database: str
    credentials_configured: dict[str, bool]
    llm_provider: str
    availability_policy: str


def create_app(settings: Settings | None = None, ctx: AppContext | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        configure_logging(settings.log_level, settings.log_json)
        app.state.ctx = ctx or AppContext(settings)
        yield
        app.state.ctx.close()

    app = FastAPI(
        title="AlphaForge",
        version=__version__,
        lifespan=lifespan,
        description="An agentic financial intelligence and quantitative research platform. "
        "v0.1: Financial Research Foundation (SEC/FRED data, company research).",
    )

    def _as_of(value: str | None) -> datetime | None:
        try:
            return parse_as_of(value)
        except ValueError as exc:
            raise HTTPException(422, f"Invalid as_of: {value!r} (use YYYY-MM-DD or ISO 8601)") from exc

    @app.exception_handler(FinanceAgentError)
    async def _domain_error(_: Request, exc: FinanceAgentError) -> JSONResponse:
        status = {
            NotFoundError: 404,
            DataUnavailableError: 422,
            ConfigurationError: 503,
            MalformedDataError: 502,
            ProviderError: 502,
        }
        code = next((c for t, c in status.items() if isinstance(exc, t)), 500)
        return JSONResponse(
            status_code=code, content={"error": type(exc).__name__, "detail": redact(str(exc))}
        )

    @app.get("/health", response_model=HealthResponse)
    def health(ctx: Ctx) -> HealthResponse:
        db_ok = ctx.db.ping()
        return HealthResponse(
            status="ok" if db_ok else "degraded",
            version=__version__,
            time=utcnow(),
            offline_mode=ctx.settings.offline_mode,
            database="ok" if db_ok else "unavailable",
            credentials_configured=ctx.settings.credential_status(),
            llm_provider=ctx.llm.name,
            availability_policy=AVAILABILITY_POLICY,
        )

    @app.get("/companies/{ticker}", response_model=CompanyOverview)
    def company(ticker: Ticker, ctx: Ctx) -> CompanyOverview:
        return ctx.company(ticker)

    @app.get("/companies/{ticker}/financials", response_model=FinancialsResponse)
    def financials(
        ticker: Ticker,
        ctx: Ctx,
        quarters: Annotated[int, Query(ge=1, le=20)] = 8,
        as_of: Annotated[str | None, Query(description="Point-in-time cutoff (YYYY-MM-DD or ISO)")] = None,
    ) -> FinancialsResponse:
        return ctx.financials(ticker, quarters, _as_of(as_of))

    @app.get("/macro/{series_id}", response_model=MacroSeriesResponse)
    def macro(
        series_id: SeriesId,
        ctx: Ctx,
        as_of: Annotated[str | None, Query()] = None,
        start: Annotated[date | None, Query()] = None,
        end: Annotated[date | None, Query()] = None,
    ) -> MacroSeriesResponse:
        if start and end and start > end:
            raise HTTPException(422, "start must be on or before end")
        return ctx.macro(series_id, as_of=_as_of(as_of), start=start, end=end)

    @app.post("/research/company", response_model=ResearchRunOut)
    def research(req: ResearchRequest, ctx: Ctx) -> ResearchRunOut:
        state = CompanyResearchWorkflow(ctx).run(req)
        return _run_out(ctx, state["run_id"])

    @app.get("/research/{run_id}", response_model=ResearchRunOut)
    def get_research(run_id: Annotated[str, Path(pattern=r"^[0-9a-f\-]{36}$")], ctx: Ctx) -> ResearchRunOut:
        return _run_out(ctx, run_id)

    return app


def _run_out(ctx: AppContext, run_id: str) -> ResearchRunOut:
    found = ctx.repo.get_run(run_id)
    if found is None:
        raise HTTPException(404, f"Research run {run_id} not found")
    run, rep = found
    return ResearchRunOut(
        run_id=run.id,
        status=run.status,
        question=run.question,
        ticker=run.ticker,
        created_at=run.created_at,
        completed_at=run.completed_at,
        error=run.error,
        steps=run.steps or [],
        report=ResearchReportOut.model_validate(rep.report_json) if rep else None,
    )
