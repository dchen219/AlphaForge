"""Time handling and the conservative information-availability policy.

Neither SEC companyfacts (``filed``) nor ALFRED (``realtime_start``) provide an
intraday publication timestamp, only a date. To avoid look-ahead bias we treat a
date-stamped source as available **only from 00:00 America/New_York on the
following calendar day**. EDGAR disseminates filings until 22:00 ET on the filing
date, and macro releases happen during the ET day, so this never marks data as
available before it could actually have been seen. The cost is that same-day
use of a release is (deliberately) impossible.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")  # resolved via the `tzdata` package on Windows

AVAILABILITY_POLICY = "date_stamped_sources_available_from_next_day_00:00_America/New_York"


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_aware(dt: datetime) -> datetime:
    """Interpret naive datetimes as UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def available_at(source_date: date) -> datetime:
    """Earliest instant (UTC) at which a date-stamped source is treated as public."""
    return datetime.combine(source_date + timedelta(days=1), time(0, 0), tzinfo=ET).astimezone(UTC)


def is_available(source_date: date, as_of: datetime | None) -> bool:
    if as_of is None:
        return True
    return available_at(source_date) <= ensure_aware(as_of)


def latest_usable_source_date(as_of: datetime) -> date:
    """Latest source date whose content is usable at ``as_of`` under the policy."""
    return ensure_aware(as_of).astimezone(ET).date() - timedelta(days=1)


def parse_window_start(value: str | datetime | date | None) -> datetime | None:
    """Window *start*: a bare date means 00:00 New York that day (cutoffs use ``parse_as_of``)."""
    if value is None or value == "":
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time(0, 0), tzinfo=ET).astimezone(UTC)
    if isinstance(value, str) and len(value.strip()) == 10:
        return parse_window_start(date.fromisoformat(value.strip()))
    return parse_as_of(value)


def parse_as_of(value: str | datetime | date | None) -> datetime | None:
    """Parse a user-supplied as_of. A bare date means end of that day in New York."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_aware(value)
    if isinstance(value, date):
        return datetime.combine(value, time(23, 59, 59), tzinfo=ET).astimezone(UTC)
    text = value.strip()
    if len(text) == 10:
        return parse_as_of(date.fromisoformat(text))
    return ensure_aware(datetime.fromisoformat(text.replace("Z", "+00:00")))
