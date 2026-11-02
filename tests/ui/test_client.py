"""API client: mocked HTTP responses, error handling, timeouts and host restrictions."""

from __future__ import annotations

import json

import httpx
import pytest

from finance_agent.ui.client import AlphaForgeClient, ApiError
from finance_agent.ui.validation import ValidationError

HEALTH = {
    "status": "ok",
    "version": "0.1.0",
    "time": "2026-11-02T00:00:00Z",
    "offline_mode": True,
    "database": "ok",
    "credentials_configured": {"fred_api_key": False, "sec_user_agent": False, "anthropic_api_key": False},
    "llm_provider": "none",
    "availability_policy": "x",
}


def client_with(handler) -> AlphaForgeClient:
    return AlphaForgeClient("http://127.0.0.1:8000", transport=httpx.MockTransport(handler))


def test_health_parses_into_typed_model():
    c = client_with(lambda req: httpx.Response(200, json=HEALTH))
    h = c.health()
    assert h.offline_mode is True and h.llm_provider == "none" and h.fixture_event_window is None


def test_query_params_drop_empty_values_and_repeat_lists():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = req.url
        return httpx.Response(200, json={"as_of": None, "count": 0, "events": []})

    client_with(handler).events(
        start="2026-10-01", categories=["MACRO_RELEASE", "CENTRAL_BANK"], ticker=None, levels=[]
    )
    q = seen["url"].params
    assert q.get_list("category") == ["MACRO_RELEASE", "CENTRAL_BANK"]
    assert q["from"] == "2026-10-01" and "ticker" not in q and "level" not in q


def test_research_posts_explicit_body():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(404, json={"detail": "nope"})

    with pytest.raises(ApiError):
        client_with(handler).research("MSFT", 4, "2025-06-30", use_llm=False)
    assert seen["body"] == {
        "ticker": "MSFT",
        "quarters": 4,
        "as_of": "2025-06-30",
        "use_llm": False,
        "question": None,
    }


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (404, {"detail": "Research run x not found"}, "Research run x not found"),
        (422, {"error": "DataUnavailableError", "detail": "no periods"}, "DataUnavailableError: no periods"),
        (
            422,
            {"detail": [{"loc": ["query", "quarters"], "msg": "must be <= 20", "type": "x"}]},
            "quarters: must be <= 20",
        ),
        (503, {"error": "ConfigurationError", "detail": "SEC_USER_AGENT must be set"}, "SEC_USER_AGENT"),
    ],
)
def test_http_errors_become_informative_api_errors(status, body, expected):
    c = client_with(lambda req: httpx.Response(status, json=body))
    with pytest.raises(ApiError) as ei:
        c.research_run("00000000-0000-0000-0000-000000000000")
    assert ei.value.kind == "http" and ei.value.status == status and expected in ei.value.message


def test_non_json_error_body():
    c = client_with(lambda req: httpx.Response(500, text="Internal Server Error"))
    with pytest.raises(ApiError) as ei:
        c.health()
    assert ei.value.status == 500 and "Internal Server Error" in ei.value.message


def test_contract_violation_is_reported_not_rendered():
    c = client_with(lambda req: httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(ApiError) as ei:
        c.health()
    assert ei.value.kind == "contract"


def test_unreachable_backend_explains_how_to_start_it():
    def handler(req):
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(ApiError) as ei:
        client_with(handler).health()
    assert ei.value.kind == "unreachable" and "finance-agent serve" in ei.value.message


def test_timeout_is_reported():
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(ApiError) as ei:
        client_with(handler).briefing(date="2026-10-28")
    assert ei.value.kind == "timeout"


def test_redirects_are_not_followed():
    c = client_with(lambda req: httpx.Response(307, headers={"location": "http://evil.example/"}))
    with pytest.raises(ApiError) as ei:
        c.health()
    assert ei.value.status == 307


def test_workflow_calls_use_long_timeout_and_reads_short():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.extensions["timeout"]["read"])
        return httpx.Response(200, json=HEALTH)

    c = client_with(handler)
    c.health()
    with pytest.raises(ApiError):  # body is not a briefing: contract error, timeout already recorded
        c.briefing(date="2026-10-28")
    assert seen[0] <= 5 and seen[1] >= 300


@pytest.mark.parametrize(
    "url", ["http://example.com:8000", "http://10.0.0.5:8000", "ftp://127.0.0.1", "http://user:pw@127.0.0.1"]
)
def test_non_loopback_or_unsafe_hosts_are_refused(url):
    with pytest.raises(ValidationError):
        AlphaForgeClient(url)


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8001/", "http://[::1]:8000"])
def test_loopback_hosts_are_accepted(url):
    AlphaForgeClient(url).close()


# ---------------------------------------------------------------- v0.3-A market + system


PRICES = {
    "instrument_id": "NVDA",
    "contract": {
        "query_mode": "point_in_time",
        "as_of": "2026-07-01T03:59:59Z",
        "basis": "split_adjusted",
        "adjustment_method": "m",
        "retrospectively_adjusted": False,
        "statement": "POINT-IN-TIME view",
    },
    "factors": [],
    "rows": [{"session_date": "2026-06-30", "close": 1.0}],
    "missing_sessions": {},
    "data_origins": ["fixture:synthetic"],
    "notes": [],
}


def test_prices_sends_explicit_basis_mode_and_cutoff():
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = req.url
        return httpx.Response(200, json=PRICES)

    res = client_with(handler).prices(
        "NVDA", "2026-06-01", "2026-06-30", basis="split_adjusted", as_of="2026-06-30"
    )
    q = seen["url"].params
    assert seen["url"].path == "/market/prices/NVDA"
    assert q["basis"] == "split_adjusted" and q["mode"] == "point_in_time" and q["as_of"] == "2026-06-30"
    assert q["include_provisional"] == "false"
    assert res.contract.basis == "split_adjusted" and res.data_origins == ["fixture:synthetic"]


def test_market_contract_drift_is_a_contract_error():
    bad = dict(PRICES)
    bad.pop("contract")
    with pytest.raises(ApiError) as exc:
        client_with(lambda req: httpx.Response(200, json=bad)).prices(
            "NVDA", "2026-06-01", "2026-06-30", basis="raw"
        )
    assert exc.value.kind == "contract"


def test_market_422_detail_is_readable():
    body = {"detail": "research mode is retrospective and does not take as_of; use point_in_time"}
    with pytest.raises(ApiError) as exc:
        client_with(lambda req: httpx.Response(422, json=body)).returns(
            "SPY", "2026-01-02", "2026-02-02", basis="raw", mode="research", as_of="2026-02-01"
        )
    assert exc.value.status == 422 and "retrospective" in exc.value.message


def test_overview_parses_credential_booleans_only():
    body = {
        "offline_mode": True,
        "sources": [
            {
                "key": "alpaca",
                "name": "Alpaca",
                "used_for": "prices",
                "credentials": [{"env_var": "ALPACA_API_KEY_ID", "configured": False}],
                "live_ready": False,
                "live_note": "",
                "offline_origin": "fixture:synthetic",
            }
        ],
        "event_store": [],
        "market_instruments": 0,
        "market_runs": [],
        "market_quality_counts": {},
        "research_runs": {},
        "briefings": {},
        "http_ingestion_enabled": False,
    }
    ov = client_with(lambda req: httpx.Response(200, json=body)).overview()
    cred = ov.sources[0].credentials[0]
    assert cred.model_dump() == {"env_var": "ALPACA_API_KEY_ID", "configured": False}
