"""Trading-session-relative windows (actual XNYS sessions, never calendar-day offsets)."""

from __future__ import annotations

from datetime import date, timedelta

from finance_agent.errors import DataUnavailableError
from finance_agent.event_studies.schemas import EstimationWindow, EventWindow
from finance_agent.market.calendar import TradingCalendar


def session_grid(calendar: TradingCalendar, day0: date, lo: int, hi: int) -> dict[int, date]:
    """Map relative offsets lo..hi to sessions (day0 must be a session)."""
    if not calendar.is_session(day0):
        raise DataUnavailableError(f"day 0 {day0} is not a trading session")
    before = int((-min(lo, 0) + 1) * 1.6) + 15
    after = int(max(hi, 0) * 1.6) + 15
    first = max(calendar.first_session, day0 - timedelta(days=before))
    last = min(calendar.last_session, day0 + timedelta(days=after))
    sessions = calendar.sessions(first, last)
    i0 = sessions.index(day0)
    if i0 + lo < 0 or i0 + hi >= len(sessions):
        raise DataUnavailableError(f"the trading calendar does not cover offsets [{lo},{hi}] around {day0}")
    return {k: sessions[i0 + k] for k in range(lo, hi + 1)}


def grid_bounds(est: EstimationWindow, windows: tuple[EventWindow, ...]) -> tuple[int, int]:
    """Offsets needed: one session before the estimation start (first return needs a prior close)."""
    lo = min(est.start, *(w.start for w in windows)) - 1
    hi = max(w.end for w in windows)
    return lo, hi
