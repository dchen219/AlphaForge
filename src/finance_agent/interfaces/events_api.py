"""v0.2 event-intelligence HTTP routes (mounted by ``interfaces.api.create_app``)."""

from __future__ import annotations

import hmac
from datetime import date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from finance_agent.events.briefing import BriefingReport, BriefingRequest
from finance_agent.events.replay import replay_audit
from finance_agent.events.schemas import (
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    ImportanceLevel,
    VerificationStatus,
)
from finance_agent.events.service import SOURCES, EventIngestor
from finance_agent.events.store import BriefingStore, EventStore
from finance_agent.timeutil import ensure_aware, parse_as_of, parse_window_start, utcnow
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
    # Every version linked through supersedes_event_id (oldest first); filtered to the cutoff
    # when ``as_of`` is given.
    revision_history: list[FinancialEvent] = Field(default_factory=list)
    as_of: datetime | None = None


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
    as_of: datetime | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    created_at: datetime | None = None
    completed_at: datetime | None = None
    params: dict = Field(default_factory=dict)


class BriefingSummary(BaseModel):
    briefing_id: str
    status: str
    as_of: datetime
    window_start: datetime
    window_end: datetime
    verification_status: str | None
    llm_status: str | None
    data_origins: list[str]
    created_at: datetime
    completed_at: datetime | None
    error: str | None


class BriefingList(BaseModel):
    count: int
    briefings: list[BriefingSummary]


class VisibilityDecision(BaseModel):
    event: FinancialEvent
    visible: bool
    reason: str
    masked_links: dict[str, str] = Field(default_factory=dict)


class CalendarEntryVisibility(BaseModel):
    event: FinancialEvent
    first_observed_at: datetime | None
    # known_upcoming | known_due_confirmed | known_due_unconfirmed | withheld_observed_later
    status: str


class ReplayOut(BaseModel):
    as_of: datetime
    window_start: datetime
    horizon_end: datetime
    visible: list[VisibilityDecision]
    excluded_future: list[VisibilityDecision]
    excluded_no_availability_time: list[VisibilityDecision]
    calendar: list[CalendarEntryVisibility]
    precision_counts: dict[str, int]
    notes: list[str]


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
    subtype: Annotated[list[EventSubtype] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> EventList:
    cut = _dt(as_of, "as_of")
    evs = EventStore(ctx.db).query(
        as_of=cut,
        start=_dt(start, "from"),
        end=_dt(end, "to"),
        categories=[str(c) for c in category or []] or None,
        subtypes=[str(x) for x in subtype or []] or None,
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


@router.get("/events/replay", response_model=ReplayOut, tags=["events"])
def replay(
    ctx: Ctx,
    as_of: Annotated[str, Query(description="Historical cutoff (YYYY-MM-DD = 23:59:59 New York, or ISO)")],
    start: Annotated[
        str | None, Query(alias="from", description="Window start; default cutoff minus 7 days")
    ] = None,
    horizon_days: Annotated[int, Query(ge=1, le=365, description="Diagnostic look past the cutoff")] = 30,
    category: Annotated[list[EventCategory] | None, Query()] = None,
    ticker: Annotated[str | None, Query(pattern=r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$")] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 300,
) -> ReplayOut:
    """Read-only visibility audit: which stored events a run at ``as_of`` sees, and why.

    ``excluded_*`` lists are look-ahead diagnostics of what the cutoff hides; never used in analysis.
    """
    cut = _dt(as_of, "as_of")
    s = _dt(start, "from") or cut - timedelta(days=7)
    if s >= cut:
        raise HTTPException(422, "'from' must be before 'as_of'")
    a = replay_audit(
        EventStore(ctx.db),
        as_of=cut,
        start=s,
        horizon_days=horizon_days,
        categories=[str(c) for c in category or []] or None,
        ticker=ticker,
        limit=limit,
    )
    cal = a.calendar
    due = {e.event_id for e in cal.due}
    unconfirmed = {e.event_id for e in cal.unconfirmed}
    withheld = {e.event_id for e in cal.withheld}

    def entry(e: FinancialEvent) -> CalendarEntryVisibility:
        if e.event_id in withheld:
            status = "withheld_observed_later"
        elif e.event_id in due:
            status = "known_due_unconfirmed" if e.event_id in unconfirmed else "known_due_confirmed"
        else:
            status = "known_upcoming"
        return CalendarEntryVisibility(event=e, first_observed_at=cal.known_at.get(e.event_id), status=status)

    def out(ds) -> list[VisibilityDecision]:
        return [
            VisibilityDecision(event=d.event, visible=d.visible, reason=d.reason, masked_links=d.masked_links)
            for d in ds
        ]

    return ReplayOut(
        as_of=a.as_of,
        window_start=a.window_start,
        horizon_end=a.horizon_end,
        visible=out(a.visible),
        excluded_future=out(a.excluded_future),
        excluded_no_availability_time=out(a.excluded_no_time),
        calendar=[entry(e) for e in cal.due + cal.upcoming + cal.withheld],
        precision_counts=a.precision_counts,
        notes=[
            "Visible events are exactly what an as_of query (and the briefing workflow) returns.",
            "Excluded lists are a look-ahead diagnostic; a run at the cutoff never sees them.",
            "Calendars are not versioned historically: a schedule counts as known only from the time "
            "a snapshot containing it was retrieved.",
        ],
    )


@router.get("/events/{event_id}", response_model=EventDetail, tags=["events"])
def get_event(
    event_id: EventId,
    ctx: Ctx,
    as_of: Annotated[
        str | None, Query(description="Point-in-time view: 404 if not yet available; later links hidden")
    ] = None,
) -> EventDetail:
    store = EventStore(ctx.db)
    ev = store.get(event_id)
    if ev is None:
        raise HTTPException(404, f"Event {event_id} not found")
    cut = _dt(as_of, "as_of")
    history = store.revision_chain(event_id)
    if cut is not None:
        if ev.event_status == EventStatus.SCHEDULED:
            known = store.schedule_known_at([event_id]).get(event_id)
            visible = known is not None and known <= cut
        else:
            visible = ev.has_occurred and ev.available_at is not None and ev.available_at <= cut
        if not visible:
            raise HTTPException(404, f"Event {event_id} was not available at {cut.isoformat()}")
        ev = store.mask_future_links([ev], cut)[0]
        history = [h for h in history if h.available_at is not None and h.available_at <= cut]

    def usable(m: FinancialEvent) -> bool:
        return cut is None or (m.available_at is not None and m.available_at <= cut)

    members = (
        [m.event_id for m in store.cluster_members(ev.event_cluster_id) if usable(m)]
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
        revision_history=history if len(history) > 1 else [],
        as_of=cut,
    )


@router.post("/intelligence/briefing", response_model=BriefingRunOut, tags=["intelligence"])
def create_briefing(req: BriefingRequest, ctx: Ctx) -> BriefingRunOut:
    if req.date:
        _dt(req.date, "date")
    state = BriefingWorkflow(ctx).run(req)
    return _briefing_out(ctx, state["briefing_id"], state)


@router.get("/intelligence/briefings", response_model=BriefingList, tags=["intelligence"])
def list_briefings(ctx: Ctx, limit: Annotated[int, Query(ge=1, le=200)] = 50) -> BriefingList:
    """Stored briefing runs, most recent first (read-only)."""
    out = []
    for r in BriefingStore(ctx.db).recent(limit):
        rep = r.report_json or {}
        out.append(
            BriefingSummary(
                briefing_id=r.briefing_id,
                status=r.status,
                as_of=ensure_aware(r.as_of),
                window_start=ensure_aware(r.window_start),
                window_end=ensure_aware(r.window_end),
                verification_status=r.verification_status,
                llm_status=(rep.get("llm") or {}).get("status"),
                data_origins=rep.get("data_origins", []),
                created_at=ensure_aware(r.created_at),
                completed_at=ensure_aware(r.completed_at) if r.completed_at else None,
                error=r.error,
            )
        )
    return BriefingList(count=len(out), briefings=out)


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
        as_of=ensure_aware(rec.as_of),
        window_start=ensure_aware(rec.window_start),
        window_end=ensure_aware(rec.window_end),
        created_at=ensure_aware(rec.created_at),
        completed_at=ensure_aware(rec.completed_at) if rec.completed_at else None,
        params=rec.params or {},
    )
