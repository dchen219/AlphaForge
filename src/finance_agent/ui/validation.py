"""Form validation for the testing console (pure functions, no Streamlit)."""

from __future__ import annotations

import ipaddress
import re
from datetime import date, datetime, time
from urllib.parse import urlsplit

from finance_agent.schemas.research import TICKER_PATTERN
from finance_agent.timeutil import ET

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_EVENT_ID = re.compile(r"^EV-[0-9a-f]{16}$")


class ValidationError(ValueError):
    """A user input that the console refuses to send."""


def ticker(value: str | None, *, required: bool = True) -> str | None:
    v = (value or "").strip().upper()
    if not v:
        if required:
            raise ValidationError("Enter a ticker symbol, e.g. MSFT.")
        return None
    if not re.match(TICKER_PATTERN, v):
        raise ValidationError(f"{value!r} is not a valid ticker (letters, digits, '.' or '-'; max 10).")
    return v


def quarters(value: int) -> int:
    if not 2 <= int(value) <= 20:
        raise ValidationError("Quarters must be between 2 and 20.")
    return int(value)


def cutoff(day: date | None, at: time | None = None) -> str | None:
    """API ``as_of`` string. A bare date keeps the backend rule (23:59:59 New York that day);
    an explicit time is interpreted in America/New_York and sent with its offset."""
    if day is None:
        return None
    if at is None:
        return day.isoformat()
    return datetime.combine(day, at, tzinfo=ET).isoformat()


def date_range(start: date | None, end: date | None) -> tuple[date | None, date | None]:
    if start and end and start > end:
        raise ValidationError("The start date must be on or before the end date.")
    return start, end


def market_range(start: date | None, end: date | None, *, max_days: int = 366 * 3) -> tuple[date, date]:
    """Bounded date range for market queries (the calendar endpoint caps ranges at 400 days)."""
    if start is None or end is None:
        raise ValidationError("Choose both a start and an end date.")
    date_range(start, end)
    if (end - start).days > max_days:
        raise ValidationError(f"Date range is limited to {max_days} days.")
    return start, end


def run_id(value: str) -> str:
    v = value.strip().lower()
    if not _UUID.match(v):
        raise ValidationError("Run/briefing ids are UUIDs, e.g. 3f2b…-…")
    return v


def event_id(value: str) -> str:
    v = value.strip()
    if not _EVENT_ID.match(v):
        raise ValidationError("Event ids look like EV- followed by 16 hex characters.")
    return v


def api_base_url(url: str) -> str:
    """Only loopback HTTP(S) backends are allowed: the console never talks to arbitrary hosts."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in {"http", "https"} or not host:
        raise ValidationError(f"API URL must be http(s)://host:port, got {url!r}.")
    loopback = host in LOOPBACK_HOSTS
    if not loopback:
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
    if not loopback:
        raise ValidationError(
            f"API host {host!r} is not a loopback address; remote backends are not allowed."
        )
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValidationError("API URL must not contain credentials, a query or a fragment.")
    return f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
