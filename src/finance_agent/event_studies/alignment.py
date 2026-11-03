"""Event-to-trading-session alignment for DAILY close-to-close returns (convention ``daily-close-v1``).

The publication time used for alignment (``event_time_for``):

* ``published_at`` (verified source timestamp: EDGAR acceptance, Fed RSS pubDate) -> VERIFIED.
* ``SCHEDULED_TIME_CONFIRMED_DATE`` events (BLS/BEA): ``available_at`` is the official scheduled
  release time on a date confirmed by ALFRED vintages -> OFFICIAL_SCHEDULE_CONFIRMED_DATE.
* ``DATE_ONLY_CONSERVATIVE`` events: ``available_at`` is D+1 00:00 New York for a source dated D;
  only the publication DATE D is known -> DATE_ONLY (``UNCERTAIN_TIME``).
* otherwise -> no usable time -> ``UNKNOWN`` (ineligible).

Daily rules (America/New_York, XNYS sessions; close boundaries from the calendar, so early closes
and DST are handled by real UTC instants):

| class | condition | day 0 |
|---|---|---|
| PRE_MARKET | t < regular open on a session day | same session |
| REGULAR_SESSION | open <= t < close | same session; the daily return includes PRE-event trading |
| AT_CLOSE | t == close | next session |
| AFTER_HOURS | t > close | next session |
| NON_TRADING_DAY | weekend/holiday | next session |
| UNCERTAIN_TIME | date D only, D a session | D; [0,0] ineligible (release may be after the close) |
| UNCERTAIN_TIME | date D only, D not a session | next session (any time on D maps there) |
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from finance_agent.event_studies.schemas import EventAlignment, TimeBasis, TimingClass
from finance_agent.events.schemas import AvailabilityPrecision, FinancialEvent
from finance_agent.market.calendar import TradingCalendar
from finance_agent.timeutil import ET


def event_time_for(event: FinancialEvent) -> tuple[datetime | None, date | None, TimeBasis]:
    """(timestamp, date-only, basis) used for alignment. Never invents a time."""
    if event.published_at is not None:
        return event.published_at, None, TimeBasis.VERIFIED_TIMESTAMP
    prec = str(event.availability_precision)
    if prec == AvailabilityPrecision.SCHEDULED_TIME_CONFIRMED_DATE and event.available_at is not None:
        return event.available_at, None, TimeBasis.OFFICIAL_SCHEDULE_CONFIRMED_DATE
    if prec == AvailabilityPrecision.DATE_ONLY_CONSERVATIVE and event.available_at is not None:
        local = event.available_at.astimezone(ET)
        d = local.date() - timedelta(days=1) if local.time() == datetime.min.time() else local.date()
        return None, d, TimeBasis.DATE_ONLY
    return None, None, TimeBasis.NONE


def align_timestamp(
    event_id: str,
    t: datetime | None,
    calendar: TradingCalendar,
    *,
    basis: TimeBasis,
    date_only: date | None = None,
) -> EventAlignment:
    if t is None and date_only is None:
        return EventAlignment(
            event_id=event_id, event_time=None, event_time_local=None, time_basis=basis,
            timing_class=TimingClass.UNKNOWN, local_date=None, day0=None, eligible=False,
            rule="no verified publication time or date: ineligible for event-time analysis",
        )  # fmt: skip
    if t is None:
        d = date_only
        if calendar.is_session(d):
            return EventAlignment(
                event_id=event_id, event_time=None, event_time_local=None, time_basis=basis,
                timing_class=TimingClass.UNCERTAIN_TIME, local_date=d, day0=d, timing_uncertain=True,
                rule="publication date known, time unknown: day 0 = the publication-date session; "
                "windows must include day +1 because the release may have followed the close",
                notes=["[0,0] windows are INELIGIBLE (timing-sensitive)."],
            )  # fmt: skip
        nxt = calendar.next_session(d)
        return EventAlignment(
            event_id=event_id, event_time=None, event_time_local=None, time_basis=basis,
            timing_class=TimingClass.NON_TRADING_DAY, local_date=d, day0=nxt,
            rule="publication date is not a trading session: any time that day maps to the next session",
        )  # fmt: skip
    if t.tzinfo is None:
        raise ValueError("event time must be timezone-aware")
    local = t.astimezone(ET)
    d = local.date()
    stamp = local.isoformat()
    notes = []
    if basis == TimeBasis.OFFICIAL_SCHEDULE_CONFIRMED_DATE:
        notes.append("time is the official scheduled release time; the date is source-confirmed")
    if not calendar.is_session(d):
        nxt = calendar.next_session(d)
        return EventAlignment(
            event_id=event_id, event_time=t, event_time_local=stamp, time_basis=basis,
            timing_class=TimingClass.NON_TRADING_DAY, local_date=d, day0=nxt,
            rule=f"{d} is not an XNYS session: day 0 = next session {nxt}", notes=notes,
        )  # fmt: skip
    s = calendar.session(d)
    if t < s.market_open:
        return EventAlignment(
            event_id=event_id, event_time=t, event_time_local=stamp, time_basis=basis,
            timing_class=TimingClass.PRE_MARKET, local_date=d, day0=d, early_close=s.early_close,
            rule="before the regular open: day 0 = same session (close-to-close return spans the release)",
            notes=notes,
        )  # fmt: skip
    if t < s.market_close:
        return EventAlignment(
            event_id=event_id, event_time=t, event_time_local=stamp, time_basis=basis,
            timing_class=TimingClass.REGULAR_SESSION, local_date=d, day0=d, early_close=s.early_close,
            contamination_warning=(
                "released during regular trading: the day-0 close-to-close return includes trading "
                "BEFORE the release (pre-announcement contamination)"
            ),
            rule="during the regular session: day 0 = same session", notes=notes,
        )  # fmt: skip
    nxt = calendar.next_session(d)
    close_local = s.market_close.astimezone(ET).strftime("%H:%M")
    if t == s.market_close:
        cls, rule = TimingClass.AT_CLOSE, f"exactly at the {close_local} ET close: day 0 = next session {nxt}"
    else:
        cls, rule = TimingClass.AFTER_HOURS, f"after the {close_local} ET close: day 0 = next session {nxt}"
    if s.early_close:
        notes.append(f"{d} was an early-close session ({close_local} ET)")
    return EventAlignment(
        event_id=event_id, event_time=t, event_time_local=stamp, time_basis=basis, timing_class=cls,
        local_date=d, day0=nxt, early_close=s.early_close, rule=rule, notes=notes,
    )  # fmt: skip


def align_event(event: FinancialEvent, calendar: TradingCalendar) -> EventAlignment:
    t, d, basis = event_time_for(event)
    return align_timestamp(event.event_id, t, calendar, basis=basis, date_only=d)
