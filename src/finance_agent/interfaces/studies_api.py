"""/event-studies routes (v0.3-B).

Running a study is CPU-bound, so ``POST`` endpoints are administrative: disabled unless
``INGEST_ADMIN_TOKEN`` is set, and then only from the local machine with a matching
``X-Admin-Token``. Requests are bounded (one target, <= 8 windows, offsets <= 500 sessions,
<= 100 events per comparison) and comparisons stop at a wall-clock budget
(``EVENT_STUDY_TIME_BUDGET_SECONDS``; checked between events). Reading stored runs is public.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from finance_agent.errors import DataUnavailableError, NotFoundError
from finance_agent.event_studies.schemas import (
    DEFAULT_WINDOWS,
    MAX_ABS_OFFSET,
    MAX_WINDOWS,
    METHODOLOGY_VERSION,
    DataMode,
    EstimationWindow,
    EventStudyRequest,
    ReturnType,
)
from finance_agent.event_studies.service import MAX_EVENTS_HARD_CAP, EventStudyService
from finance_agent.interfaces.market_api import LOOPBACK
from finance_agent.timeutil import parse_as_of, parse_window_start

router = APIRouter(prefix="/event-studies", tags=["event-studies"])
RunId = Annotated[str, Path(pattern=r"^ES-[0-9a-f]{16}$")]


def get_ctx(request: Request):
    return request.app.state.ctx


Ctx = Annotated[Any, Depends(get_ctx)]


def _admin(ctx, request: Request, token_header: str | None) -> None:
    token = ctx.settings.ingest_admin_token
    if token is None or not token.get_secret_value():
        raise HTTPException(403, "Running studies over HTTP is disabled; use the CLI (study run).")
    if (request.client.host if request.client else "") not in LOOPBACK:
        raise HTTPException(403, "Studies are only run for requests from the local machine.")
    if not token_header or not hmac.compare_digest(token_header, token.get_secret_value()):
        raise HTTPException(401, "Invalid or missing X-Admin-Token.")


def _call(fn):
    try:
        return fn()
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (DataUnavailableError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


def _cut(v: str | None):
    try:
        return parse_as_of(v)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid as_of: {v!r}") from exc


class StudyBody(BaseModel):
    event_id: str = Field(max_length=40)
    ticker: str | None = Field(default=None, max_length=12)
    series_id: str | None = Field(default=None, max_length=12)
    benchmark: str | None = Field(default="SPY", max_length=12)
    model: str = "market-model"
    price_basis: str | None = None  # required for equity studies (explicit basis)
    return_type: ReturnType = ReturnType.SIMPLE
    data_mode: DataMode = DataMode.RESEARCH
    as_of: str | None = None
    windows: list[str] | None = Field(default=None, max_length=MAX_WINDOWS)
    estimation_start: int = Field(default=-150, ge=-MAX_ABS_OFFSET, le=-2)
    estimation_end: int = Field(default=-21, ge=-MAX_ABS_OFFSET, le=-1)
    min_observations: int = Field(default=100, ge=10, le=MAX_ABS_OFFSET)
    robustness: bool = True


class CompareBody(BaseModel):
    event_type: str
    ticker: str = Field(max_length=12)
    benchmark: str = Field(default="SPY", max_length=12)
    subtypes: list[str] | None = Field(default=None, max_length=10)
    model: str = "market-model"
    price_basis: str = "total_return"
    start: str | None = None
    end: str | None = None
    windows: list[str] | None = Field(default=None, max_length=MAX_WINDOWS)
    data_mode: DataMode = DataMode.RESEARCH
    as_of: str | None = None
    max_events: int = Field(default=40, ge=1, le=MAX_EVENTS_HARD_CAP)
    first_release_only: bool = True


@router.get("/specifications")
def specifications() -> dict:
    est = EstimationWindow()
    return {
        "methodology_version": METHODOLOGY_VERSION,
        "models": {
            "raw": "Model A: observed returns (descriptive, no test)",
            "market-adjusted": "Model B: AR = R_i - R_m (no estimation; Var from the estimation window)",
            "market-model": "Model C: OLS R_i = alpha + beta R_m over the estimation window",
        },
        "price_bases": ["raw", "split_adjusted", "total_return"],
        "return_types": ["simple", "log"],
        "data_modes": {
            "research": "retrospective descriptive research (latest data, all corporate actions known now)",
            "point_in_time": "only data/actions available at as_of; UNVERIFIED when availability is assumed",
        },
        "defaults": {
            "estimation_window": est.model_dump(),
            "event_windows": [w.label for w in DEFAULT_WINDOWS],
            "benchmark": "SPY",
            "alignment": "daily-close-v1 (equities), yield-close-v1 (~15:30 ET quotes, Treasury yields)",
        },
        "limits": {
            "max_windows": MAX_WINDOWS,
            "max_abs_offset_sessions": MAX_ABS_OFFSET,
            "max_events_per_comparison": MAX_EVENTS_HARD_CAP,
            "execution": "POST requires INGEST_ADMIN_TOKEN + loopback; comparisons stop at "
            "EVENT_STUDY_TIME_BUDGET_SECONDS",
        },
        "yield_series": ["DGS2", "DGS10"],
    }


@router.post("")
def run_study(
    body: StudyBody, request: Request, ctx: Ctx, x_admin_token: Annotated[str | None, Header()] = None
) -> dict:
    _admin(ctx, request, x_admin_token)
    if body.ticker and not body.price_basis:
        raise HTTPException(422, "price_basis is required (raw | split_adjusted | total_return)")
    req = _call(
        lambda: EventStudyRequest(
            **{**body.model_dump(exclude={"as_of", "price_basis"}),
               "price_basis": body.price_basis or "total_return", "as_of": _cut(body.as_of)}
        )
    )  # fmt: skip
    run = _call(lambda: EventStudyService(ctx).run(req))
    return _summary(run)


@router.post("/compare")
def compare(
    body: CompareBody, request: Request, ctx: Ctx, x_admin_token: Annotated[str | None, Header()] = None
) -> dict:
    _admin(ctx, request, x_admin_token)
    try:
        start = parse_window_start(body.start) if body.start else None
        end = parse_as_of(body.end) if body.end else None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    run = _call(
        lambda: EventStudyService(ctx).compare(
            category=body.event_type, ticker=body.ticker, benchmark=body.benchmark, subtypes=body.subtypes,
            model=body.model, price_basis=body.price_basis, start=start, end=end, windows=body.windows,
            data_mode=body.data_mode, as_of=_cut(body.as_of), max_events=body.max_events,
            first_release_only=body.first_release_only,
        )
    )  # fmt: skip
    return _summary(run)


def _summary(run) -> dict:
    return {
        "run_id": run.run_id,
        "kind": run.kind,
        "status": str(run.status),
        "existing": run.existing,
        "data_origin": run.data_origin,
        "synthetic": run.synthetic,
        "notice": "SYNTHETIC - methodological validation only; not an empirical finding"
        if run.synthetic
        else None,
        "spec_hash": run.spec_hash,
        "data_digest": run.data_digest,
        "links": {
            "run": f"/event-studies/{run.run_id}",
            "results": f"/event-studies/{run.run_id}/results",
            "report": f"/event-studies/{run.run_id}/report",
        },
    }


@router.get("")
def list_runs(ctx: Ctx, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> dict:
    return {"runs": EventStudyService(ctx).store.list(limit)}


@router.get("/{run_id}")
def get_run(run_id: RunId, ctx: Ctx) -> dict:
    run = _call(lambda: EventStudyService(ctx).get(run_id))
    return {**_summary(run), **run.model_dump(mode="json", exclude={"report_markdown"})}


@router.get("/{run_id}/results")
def results(run_id: RunId, ctx: Ctx) -> dict:
    svc = EventStudyService(ctx)
    run = _call(lambda: svc.get(run_id))
    return {**_summary(run), "results": svc.store.results(run_id)}


@router.get("/{run_id}/report", response_model=None)
def report(run_id: RunId, ctx: Ctx, format: Annotated[str, Query(pattern="^(json|markdown)$")] = "json"):
    rep = _call(lambda: EventStudyService(ctx).report(run_id))
    if format == "markdown":
        return PlainTextResponse(rep["markdown"], media_type="text/markdown; charset=utf-8")
    return rep
