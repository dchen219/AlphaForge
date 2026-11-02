"""Exchange trading calendars (NYSE/Nasdaq regular sessions) and event-to-session alignment.

Session boundaries come from the maintained ``exchange_calendars`` library (XNYS), which
encodes weekends, exchange holidays, special closures (e.g. national days of mourning) and
early closes. Boundaries are real UTC instants per session — never a constant UTC offset — so
daylight-saving transitions are handled by the calendar data, not by assumption.

Extended hours (US equities convention): pre-market 04:00 ET → open; after-hours close → 20:00
ET (17:00 ET on early-close days, four hours after the early close).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

from finance_agent.errors import DataUnavailableError
from finance_agent.market.schemas import SessionPhase, TradingSession
from finance_agent.timeutil import ET

DEFAULT_CALENDAR = "XNYS"  # NYSE; Nasdaq (XNAS) regular sessions are identical for US equities
PRE_MARKET_OPEN = time(4, 0)
AFTER_HOURS_SPAN = timedelta(hours=4)


@lru_cache(maxsize=4)
def _xcal(name: str):
    import exchange_calendars as xc

    return xc.get_calendar(name, start="2000-01-03")


def _ts(d: date):
    import pandas as pd

    return pd.Timestamp(d)


def _py(ts) -> datetime:
    return ts.to_pydatetime().astimezone(UTC)


@dataclass(frozen=True)
class EventAlignment:
    """Where an event timestamp falls relative to the trading calendar."""

    event_time: datetime
    phase: SessionPhase
    local_date: date  # America/New_York date of the event
    event_session: date | None  # session whose REGULAR hours contain the event (else None)
    first_reaction_session: date  # first session whose regular open is after the event
    note: str


class TradingCalendar:
    def __init__(self, name: str = DEFAULT_CALENDAR) -> None:
        self.name = name
        self._cal = _xcal(name)

    # ------------------------------------------------------------------ bounds
    @property
    def first_session(self) -> date:
        return self._cal.first_session.date()

    @property
    def last_session(self) -> date:
        return self._cal.last_session.date()

    def _check(self, d: date) -> None:
        if d < self.first_session or d > self.last_session:
            raise DataUnavailableError(
                f"{d} is outside the {self.name} calendar range "
                f"{self.first_session}..{self.last_session} (update exchange_calendars)."
            )

    # ------------------------------------------------------------------ sessions
    def is_session(self, d: date) -> bool:
        self._check(d)
        return bool(self._cal.is_session(_ts(d)))

    def sessions(self, start: date, end: date) -> list[date]:
        self._check(start)
        self._check(end)
        if end < start:
            return []
        return [s.date() for s in self._cal.sessions_in_range(_ts(start), _ts(end))]

    def previous_session(self, d: date) -> date:
        """Last session strictly before ``d``."""
        self._check(d)
        return self._cal.date_to_session(_ts(d - timedelta(days=1)), direction="previous").date()

    def next_session(self, d: date) -> date:
        """First session strictly after ``d``."""
        self._check(d)
        return self._cal.date_to_session(_ts(d + timedelta(days=1)), direction="next").date()

    def session(self, d: date) -> TradingSession:
        if not self.is_session(d):
            raise DataUnavailableError(f"{d} is not a {self.name} trading session")
        t = _ts(d)
        open_, close = _py(self._cal.session_open(t)), _py(self._cal.session_close(t))
        early = bool(close.astimezone(ET).time() < time(16, 0))
        return TradingSession(
            calendar=self.name,
            session_date=d,
            market_open=open_,
            market_close=close,
            pre_market_open=datetime.combine(d, PRE_MARKET_OPEN, tzinfo=ET).astimezone(UTC),
            after_hours_close=close + AFTER_HOURS_SPAN,
            early_close=early,
        )

    def session_table(self, start: date, end: date) -> list[TradingSession]:
        return [self.session(d) for d in self.sessions(start, end)]

    # ------------------------------------------------------------------ alignment
    def align(self, event_time: datetime) -> EventAlignment:
        """Map an event publication time to pre-market / regular / after-hours / non-trading."""
        if event_time.tzinfo is None:
            raise ValueError("event_time must be timezone-aware")
        local = event_time.astimezone(ET)
        d = local.date()
        if not self.is_session(d):
            nxt = self.next_session(d)
            return EventAlignment(
                event_time, SessionPhase.NON_TRADING_DAY, d, None, nxt,
                f"{d} is not a trading session; first reaction session {nxt}.",
            )  # fmt: skip
        s = self.session(d)
        t = event_time.astimezone(UTC)
        if t < s.pre_market_open:
            return EventAlignment(
                event_time, SessionPhase.CLOSED, d, None, d, "Before pre-market; first reaction at the open."
            )
        if t < s.market_open:
            return EventAlignment(
                event_time,
                SessionPhase.PRE_MARKET,
                d,
                None,
                d,
                "Pre-market; first regular reaction at the open.",
            )
        if t < s.market_close:
            nxt = self.next_session(d)
            return EventAlignment(
                event_time, SessionPhase.REGULAR, d, d, nxt,
                "During regular hours: the same-day daily bar is only partially after the event.",
            )  # fmt: skip
        nxt = self.next_session(d)
        phase = SessionPhase.AFTER_HOURS if t < s.after_hours_close else SessionPhase.CLOSED
        note = (
            "After the close (early close)" if s.early_close else "After the close"
        ) + f"; NOT part of {d}'s regular session — first reaction session {nxt}."
        return EventAlignment(event_time, phase, d, None, nxt, note)


@lru_cache(maxsize=4)
def get_calendar(name: str = DEFAULT_CALENDAR) -> TradingCalendar:
    return TradingCalendar(name)
