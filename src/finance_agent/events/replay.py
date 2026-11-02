"""Point-in-time visibility audit for historical replay (read-only).

Explains *why* each event is or is not visible at a cutoff, using the same store queries that
briefings use (``EventStore.query`` / ``calendar`` / ``schedule_known_at``). Nothing here decides
visibility independently: visible events are exactly what an ``as_of`` query returns.

The "excluded" lists are a diagnostic: they show stored events that a run at the cutoff must not
see (published later, or without a usable availability time). They are never fed into analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from finance_agent.events.schemas import EventStatus, FinancialEvent
from finance_agent.events.store import EventStore
from finance_agent.timeutil import ensure_aware

LINK_FIELDS = ("related_event_id", "supersedes_event_id", "event_cluster_id")


@dataclass
class CalendarVisibility:
    upcoming: list[FinancialEvent]  # scheduled in (as_of, as_of + 7d], known by the cutoff
    due: list[FinancialEvent]  # scheduled in (start, as_of], known by the cutoff
    unconfirmed: list[FinancialEvent]  # due entries without a linked publication by the cutoff
    withheld: list[FinancialEvent]  # schedule first observed after the cutoff (look-ahead)
    known_at: dict[str, datetime] = field(default_factory=dict)


def calendar_visibility(
    store: EventStore, start: datetime, as_of: datetime, events: list[FinancialEvent]
) -> CalendarVisibility:
    """Calendar entries as known at ``as_of``.

    A schedule is knowledge only once a calendar snapshot containing it was retrieved
    (calendars are not versioned historically), so later-observed entries are withheld. A due
    entry occurred only if a linked publication in ``events`` is available by the cutoff.
    """
    upcoming = store.calendar(start=as_of, end=as_of + timedelta(days=7))
    due = store.calendar(start=start, end=as_of)
    known = store.schedule_known_at([e.event_id for e in upcoming + due])
    visible = {eid for eid, t in known.items() if t <= as_of}
    withheld = [e for e in upcoming + due if e.event_id not in visible]
    upcoming = [e for e in upcoming if e.event_id in visible]
    due = [e for e in due if e.event_id in visible]
    linked = {e.related_event_id for e in events if e.related_event_id}
    unconfirmed = [d for d in due if d.event_id not in linked]
    return CalendarVisibility(upcoming, due, unconfirmed, withheld, known)


@dataclass
class Decision:
    event: FinancialEvent
    visible: bool
    reason: str
    masked_links: dict[str, str] = field(default_factory=dict)  # field -> hidden value


@dataclass
class ReplayAudit:
    as_of: datetime
    window_start: datetime
    horizon_end: datetime
    visible: list[Decision]
    excluded_future: list[Decision]
    excluded_no_time: list[Decision]
    calendar: CalendarVisibility
    precision_counts: dict[str, int]


def _ts(dt: datetime | None) -> str:
    return dt.isoformat() if dt else "unknown"


def replay_audit(
    store: EventStore,
    *,
    as_of: datetime,
    start: datetime,
    horizon_days: int = 30,
    categories: list[str] | None = None,
    ticker: str | None = None,
    limit: int = 500,
) -> ReplayAudit:
    as_of = ensure_aware(as_of)
    start = ensure_aware(start)
    horizon = as_of + timedelta(days=horizon_days)
    common = {"categories": categories, "ticker": ticker, "limit": limit}

    visible = store.query(as_of=as_of, start=start, end=as_of, **common)
    # Same rows without point-in-time masking: the difference is what masking hid.
    raw = {e.event_id: e for e in store.query(start=start, end=as_of, **common)}
    decisions = []
    for e in visible:
        masked = {
            f: getattr(raw[e.event_id], f)
            for f in LINK_FIELDS
            if e.event_id in raw and getattr(raw[e.event_id], f) and getattr(e, f) is None
        }
        reason = (
            f"available_at {_ts(e.available_at)} <= cutoff {as_of.isoformat()} "
            f"(precision {e.availability_precision})"
        )
        if masked:
            reason += "; links to information published after the cutoff were masked"
        decisions.append(Decision(e, True, reason, masked))

    future = [
        Decision(
            e,
            False,
            f"available_at {_ts(e.available_at)} is after the cutoff by "
            f"{(e.available_at - as_of) if e.available_at else 'unknown'}",
        )
        for e in store.query(start=as_of, end=horizon, **common)
    ]
    no_time = [
        Decision(e, False, "published but availability time unknown: never usable in historical replay")
        for e in store.query(
            occurred_only=False,
            statuses=[EventStatus.PUBLISHED, EventStatus.REVISED],
            **common,
        )
        if e.available_at is None
    ]
    counts: dict[str, int] = {}
    for e in visible:
        counts[str(e.availability_precision)] = counts.get(str(e.availability_precision), 0) + 1
    return ReplayAudit(
        as_of=as_of,
        window_start=start,
        horizon_end=horizon,
        visible=decisions,
        excluded_future=future,
        excluded_no_time=no_time,
        calendar=calendar_visibility(store, start, as_of, visible),
        precision_counts=counts,
    )
