"""Time policy, HTTP retry/timeout policy, fixture transport, logging redaction, config."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import httpx
import pytest

from finance_agent.config.logging import JsonFormatter, RedactingFilter, redact
from finance_agent.errors import NotFoundError, ProviderError, ProviderHTTPError, RateLimitedError
from finance_agent.http import FixtureTransport, HttpClient
from finance_agent.timeutil import available_at, is_available, latest_usable_source_date, parse_as_of
from tests.conftest import FIXTURES, offline_settings

# ---------------------------------------------------------------- availability policy


def test_available_at_is_next_day_midnight_new_york_standard_time():
    # 2024-01-30 filing -> 2024-01-31 00:00 EST = 05:00 UTC
    assert available_at(date(2024, 1, 30)) == datetime(2024, 1, 31, 5, 0, tzinfo=UTC)


def test_available_at_handles_daylight_saving():
    # 2024-07-30 filing -> 2024-07-31 00:00 EDT = 04:00 UTC
    assert available_at(date(2024, 7, 30)) == datetime(2024, 7, 31, 4, 0, tzinfo=UTC)


def test_is_available_boundaries():
    d = date(2024, 7, 30)
    assert not is_available(d, datetime(2024, 7, 30, 23, 59, tzinfo=UTC))  # same day: never
    assert not is_available(d, datetime(2024, 7, 31, 3, 59, tzinfo=UTC))
    assert is_available(d, datetime(2024, 7, 31, 4, 0, tzinfo=UTC))
    assert is_available(d, None)


def test_parse_as_of_date_means_end_of_day_new_york():
    as_of = parse_as_of("2024-07-31")
    assert as_of == datetime(2024, 8, 1, 3, 59, 59, tzinfo=UTC)
    assert latest_usable_source_date(as_of) == date(2024, 7, 30)
    assert parse_as_of("2024-07-31T12:00:00Z") == datetime(2024, 7, 31, 12, tzinfo=UTC)
    assert parse_as_of(None) is None
    with pytest.raises(ValueError):
        parse_as_of("not-a-date")


# ---------------------------------------------------------------- HTTP


def _client(handler, **kw) -> tuple[HttpClient, list[float]]:
    sleeps: list[float] = []
    c = HttpClient(
        user_agent="test-agent test@example.com",
        transport=httpx.MockTransport(handler),
        sleep=sleeps.append,
        backoff_base=0.01,
        **kw,
    )
    return c, sleeps


def test_retries_transient_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, json={"ok": True})

    c, sleeps = _client(handler)
    res = c.get("https://data.sec.gov/x.json")
    assert res.json() == {"ok": True} and calls["n"] == 3 and len(sleeps) == 2
    assert res.origin == "live" and len(res.sha256) == 64


def test_429_honours_retry_after_and_raises_after_exhaustion():
    def handler(req):
        return httpx.Response(429, headers={"retry-after": "7"})

    c, sleeps = _client(handler, max_attempts=3)
    with pytest.raises(RateLimitedError):
        c.get("https://data.sec.gov/x.json")
    assert sleeps == [7.0, 7.0]


def test_404_is_not_retried_and_maps_to_not_found():
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        return httpx.Response(404)

    c, _ = _client(handler)
    with pytest.raises(NotFoundError):
        c.get("https://data.sec.gov/missing.json")
    assert calls["n"] == 1


def test_non_retryable_4xx_raises_http_error_with_redacted_url():
    def handler(req):
        return httpx.Response(400, text="bad request api_key=SECRETKEY123")

    c, _ = _client(handler)
    with pytest.raises(ProviderHTTPError) as ei:
        c.get("https://api.stlouisfed.org/fred/series", params={"api_key": "SECRETKEY123"})
    assert "SECRETKEY123" not in str(ei.value)


def test_transport_errors_are_retried_then_reported():
    def handler(req):
        raise httpx.ConnectTimeout("timeout")

    c, sleeps = _client(handler, max_attempts=2)
    with pytest.raises(ProviderError):
        c.get("https://data.sec.gov/x.json")
    assert len(sleeps) == 1


def test_fetch_result_url_is_redacted():
    c, _ = _client(lambda req: httpx.Response(200, json={}))
    res = c.get("https://api.stlouisfed.org/fred/series", params={"api_key": "abc123", "series_id": "GDP"})
    assert "abc123" not in res.url and "series_id=GDP" in res.url


def test_fixture_transport_maps_urls_and_labels_origin():
    t = FixtureTransport(FIXTURES)
    c = HttpClient(user_agent="x", transport=t)
    res = c.get("https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json")
    assert res.origin == "fixture:real_snapshot"
    res = c.get("https://api.stlouisfed.org/fred/series/observations", params={"series_id": "GDP"})
    assert res.origin == "fixture:synthetic"
    with pytest.raises(NotFoundError, match="offline fixtures"):
        c.get("https://data.sec.gov/submissions/CIK0000000099.json")


# ---------------------------------------------------------------- logging / config


@pytest.mark.parametrize(
    "text,secret",
    [
        ("GET https://api.stlouisfed.org/fred/series?api_key=abcdef0123&x=1", "abcdef0123"),
        ("key sk-ant-api03-ABCDEFGHIJKLMNOP used", "ABCDEFGHIJKLMNOP"),
        ("headers {'x-api-key': 'zzz-secret'}", "zzz-secret"),
        ("Authorization: Bearer tok_123456", "tok_123456"),
    ],
)
def test_redact_removes_secrets(text, secret):
    assert secret not in redact(text)


def test_log_records_are_redacted(caplog):
    logger = logging.getLogger("finance_agent.test")
    record = logger.makeRecord("t", logging.INFO, __file__, 1, "url=%s", ("http://x?api_key=SEKRIT",), None)
    RedactingFilter().filter(record)
    out = JsonFormatter().format(record)
    assert "SEKRIT" not in out and "REDACTED" in out


def test_settings_never_render_secrets(tmp_path):
    s = offline_settings(tmp_path, fred_api_key="fred-secret-value", anthropic_api_key="sk-ant-xyz")
    assert "fred-secret-value" not in repr(s) and "sk-ant-xyz" not in repr(s)
    assert s.credential_status() == {"fred_api_key": True, "sec_user_agent": False, "anthropic_api_key": True}
