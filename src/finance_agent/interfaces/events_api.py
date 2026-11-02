"""v0.2 event-intelligence HTTP routes (mounted by ``interfaces.api.create_app``)."""

from __future__ import annotations

import hmac
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from finance_agent.events.briefing import BriefingReport, BriefingRequest
from finance_agent.events.schemas import EventCategory, FinancialEvent, ImportanceLevel, VerificationStatus
from finance_agent.events.service import SOURCES, EventIngestor
from finance_agent.events.store import BriefingStore, EventStore
from finance_agent.timeutil import parse_as_of, parse_window_start, utcnow
from finance_agent.workflows.briefing import BriefingWorkflow

router = APIRouter()
EventId = Annotated[str, Path(pattern=r"^EV-[0-9a-f]{16}$")]
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


def get_ctx(request: Request):
    return request.app.state.ctx


Ctx = Annotated[object, Depends(get_ctx)]


def _dt(value: str | None, name: str) -> datetime | None:
    try:
        return parse_window_start(value) if name == "from" else parse_as_of(value)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid {name}: {value!r} (use YYYY-MM-DD or ISO 8601)") from exc


class EventSourceOut(BaseModel):
    source_key: str
    source_name: str
    role: str
    source_url: str | None
    snapshot_sha256: str | None
    retrieved_at: datetime | None
    excerpt: str | None


class EventDetail(BaseModel):
    event: FinancialEvent
    sources: list[EventSourceOut]
    cluster_members: list[str]
    supersedes: FinancialEvent | None = None


class EventList(BaseModel):
    as_of: datetime | None
    count: int
    events: list[FinancialEvent]


class BriefingRunOut(BaseModel):
    briefing_id: str
    status: str
    error: str | None = None
    steps: list[dict] = Field(default_factory=list)
    report: BriefingReport | None = None


class IngestRequest(BaseModel):
    sources: list[str] = Field(default_factory=lambda: list(SOURCES))
    as_of: datetime | None = None
    start: str | None = None
    tickers: list[str] | None = None


@router.get("/events", response_model=EventList, tags=["events"])
def list_events(
    ctx: Ctx,
    start: Annotated[
        str | None, Query(alias="from", description="Window start on available_at (exclusive)")
    ] = None,
    end: Annotated[
        str | None, Query(alias="to", description="Window end on available_at (inclusive)")
    ] = None,
    as_of: Annotated[
        str | None, Query(description="Historical cutoff: hide anything available later")
    ] = None,
    category: Annotated[list[EventCategory] | None, Query()] = None,
    ticker: Annotated[str | None, Query(pattern=r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")] = None,
    level: Annotated[list[ImportanceLevel] | None, Query()] = None,
    verification: Annotated[list[VerificationStatus] | None, Query()] = None,
    source: Annotated[str | None, Query(max_length=64)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> EventList:
    cut = _dt(as_of, "as_of")
    evs = EventStore(ctx.db).query(
        as_of=cut,
        start=_dt(start, "from"),
        end=_dt(end, "to"),
        categories=[str(c) for c in category or []] or None,
        ticker=ticker,
        levels=[str(x) for x in level or []] or None,
        verification=[str(v) for v in verification or []] or None,
        source=source,
        limit=limit,
    )
    return EventList(as_of=cut, count=len(evs), events=evs)


@router.get("/events/important", response_model=EventList, tags=["events"])
def important_events(
    ctx: Ctx,
    as_of: Annotated[str | None, Query()] = None,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
    min_level: Annotated[ImportanceLevel, Query()] = ImportanceLevel.HIGH,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
) -> EventList:
    cut = _dt(as_of, "as_of") or utcnow()
    levels = (
        ["HIGH"]
        if min_level == ImportanceLevel.HIGH
        else ["HIGH", "MEDIUM"]
        if min_level == ImportanceLevel.MEDIUM
        else None
    )
    evs = EventStore(ctx.db).query(
        as_of=cut, start=cut - timedelta(days=days), end=cut, levels=levels, limit=2000
    )
    evs.sort(key=lambda e: (-(e.importance_score or -1), e.available_at))
    return EventList(as_of=cut, count=min(len(evs), limit), events=evs[:limit])


@router.get("/events/calendar", response_model=EventList, tags=["events"])
def event_calendar(
    ctx: Ctx,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> EventList:
    """Scheduled entries only. A calendar entry is NOT evidence that a release occurred."""
    s = _dt(start, "from") or utcnow()
    e = _dt(end, "to") or s + timedelta(days=30)
    if e <= s:
        raise HTTPException(422, "'to' must be after 'from'")
    evs = EventStore(ctx.db).calendar(start=s, end=e, limit=limit)
    return EventList(as_of=None, count=len(evs), events=evs)


@router.get("/events/{event_id}", response_model=EventDetail, tags=["events"])
def get_event(event_id: EventId, ctx: Ctx) -> EventDetail:
    store = EventStore(ctx.db)
    ev = store.get(event_id)
    if ev is None:
        raise HTTPException(404, f"Event {event_id} not found")
    members = (
        [m.event_id for m in store.cluster_members(ev.event_cluster_id)]
        if ev.event_cluster_id
        else [ev.event_id]
    )
    return EventDetail(
        event=ev,
        sources=[
            EventSourceOut(**{k: getattr(r, k) for k in EventSourceOut.model_fields})
            for r in store.sources(event_id)
        ],
        cluster_members=sorted(members),
        supersedes=store.get(ev.supersedes_event_id) if ev.supersedes_event_id else None,
    )


@router.post("/intelligence/briefing", response_model=BriefingRunOut, tags=["intelligence"])
def create_briefing(req: BriefingRequest, ctx: Ctx) -> BriefingRunOut:
    if req.date:
        _dt(req.date, "date")
    state = BriefingWorkflow(ctx).run(req)
    return _briefing_out(ctx, state["briefing_id"], state)


@router.get("/intelligence/briefings/{briefing_id}", response_model=BriefingRunOut, tags=["intelligence"])
def get_briefing(briefing_id: Annotated[str, Path(pattern=r"^[0-9a-f\-]{36}$")], ctx: Ctx) -> BriefingRunOut:
    return _briefing_out(ctx, briefing_id, None)


@router.post("/intelligence/ingest", tags=["intelligence"])
def ingest(
    req: IngestRequest, request: Request, ctx: Ctx, x_admin_token: Annotated[str | None, Header()] = None
) -> dict:
    """Administrative ingestion (network-heavy). Disabled unless INGEST_ADMIN_TOKEN is set;
    requires a matching X-Admin-Token header and a loopback client."""
    token = ctx.settings.ingest_admin_token
    if token is None or not token.get_secret_value():
        raise HTTPException(
            403, "Ingestion over HTTP is disabled; use the CLI (finance-agent events ingest)."
        )
    client = request.client.host if request.client else ""
    if client not in LOOPBACK:
        raise HTTPException(403, "Ingestion is only accepted from the local machine.")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, token.get_secret_value()):
        raise HTTPException(401, "Invalid or missing X-Admin-Token.")
    bad = [s for s in req.sources if s not in SOURCES]
    if bad:
        raise HTTPException(422, f"Unknown sources {bad}; allowed: {list(SOURCES)}")
    start = date.fromisoformat(req.start) if req.start else None
    return EventIngestor(ctx).run(req.sources, as_of=req.as_of, start=start, tickers=req.tickers).to_dict()


def _briefing_out(ctx, briefing_id: str, state: dict | None) -> BriefingRunOut:
    rec = BriefingStore(ctx.db).get(briefing_id)
    if rec is None:
        if state and state.get("error"):
            return BriefingRunOut(
                briefing_id=briefing_id, status="failed", error=state["error"], steps=state.get("steps", [])
            )
        raise HTTPException(404, f"Briefing {briefing_id} not found")
    return BriefingRunOut(
        briefing_id=rec.briefing_id,
        status=rec.status,
        error=rec.error,
        steps=rec.steps or [],
        report=BriefingReport.model_validate(rec.report_json) if rec.report_json else None,
    )
