"""Regression tests for defects found while building v0.2."""

from __future__ import annotations

from datetime import UTC, date, datetime

from finance_agent.http import FixtureTransport, HttpClient
from finance_agent.timeutil import parse_as_of, parse_window_start
from tests.conftest import FIXTURES


def test_window_start_date_means_start_of_new_york_day():
    """Before: API/CLI 'from=2026-10-28' meant 23:59:59 that day, hiding the whole day."""
    assert parse_window_start("2026-10-28") == datetime(2026, 10, 28, 4, 0, tzinfo=UTC)  # 00:00 EDT
    assert parse_window_start(date(2026, 12, 1)) == datetime(2026, 12, 1, 5, 0, tzinfo=UTC)  # 00:00 EST
    assert parse_as_of("2026-10-28") == datetime(2026, 10, 29, 3, 59, 59, tzinfo=UTC)  # cutoffs unchanged
    assert parse_window_start("2026-10-28T12:00:00Z") == datetime(2026, 10, 28, 12, tzinfo=UTC)
    assert parse_window_start(None) is None


def test_offline_fixture_mode_does_not_rate_limit():
    """Before: ALFRED's 0.5 req/s limit also slept during offline replay (~12 minutes)."""
    offline = HttpClient(
        user_agent="x", transport=FixtureTransport(FIXTURES), rate_limits={"alfred.stlouisfed.org": 0.5}
    )
    assert offline._limiters == {}
    live = HttpClient(user_agent="x", rate_limits={"alfred.stlouisfed.org": 0.5})
    assert "alfred.stlouisfed.org" in live._limiters
    live.close()
    offline.close()
