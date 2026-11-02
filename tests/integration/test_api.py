"""HTTP API: endpoints, input validation and credential hygiene (offline fixtures)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finance_agent.interfaces.api import create_app
from finance_agent.services import AppContext
from finance_agent.storage.db import Database
from tests.conftest import offline_settings

SECRETS = {"fred_api_key": "fred-SECRET-123", "anthropic_api_key": "sk-ant-SECRET-456"}


@pytest.fixture
def client(tmp_path):
    s = offline_settings(tmp_path, **SECRETS)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    from finance_agent.agents.llm import NullLLM

    ctx.llm = NullLLM("disabled in tests")
    with TestClient(create_app(s, ctx)) as c:
        yield c


def test_health_reports_status_without_secrets(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["database"] == "ok" and body["offline_mode"] is True
    assert body["credentials_configured"] == {
        "fred_api_key": True,
        "sec_user_agent": False,
        "anthropic_api_key": True,
    }
    for secret in SECRETS.values():
        assert secret not in r.text


def test_company_endpoint(client):
    r = client.get("/companies/msft")
    assert r.status_code == 200
    body = r.json()
    assert body["ticker"] == "MSFT" and body["profile"]["cik"] == "0000789019"
    assert body["data_origin"] == "fixture:real_snapshot" and body["recent_filings"]


@pytest.mark.parametrize(
    "path,code",
    [
        ("/companies/1BAD", 422),
        ("/companies/ZZZZZ", 404),
        ("/companies/MSFT/financials?quarters=0", 422),
        ("/companies/MSFT/financials?quarters=21", 422),
        ("/companies/MSFT/financials?as_of=notadate", 422),
        ("/macro/bad-id!", 422),
        ("/macro/NOSUCHSERIES", 404),
        ("/macro/GDP?start=2024-05-01&end=2024-01-01", 422),
        ("/research/not-a-uuid", 422),
        ("/research/00000000-0000-0000-0000-000000000000", 404),
    ],
)
def test_input_validation_and_errors(client, path, code):
    assert client.get(path).status_code == code


def test_financials_endpoint_with_as_of(client):
    r = client.get("/companies/AAPL/financials", params={"quarters": 4, "as_of": "2025-06-30"})
    assert r.status_code == 200
    fin = r.json()["financials"]
    assert fin["focus_quarters"] == 4 and fin["quarters"][-1]["end"] <= "2025-06-30"
    for row in fin["series"].values():
        for v in row:
            if v:
                assert all(s["filed"] < "2025-06-30" for s in v["sources"])


def test_macro_endpoint(client):
    r = client.get("/macro/cpiaucsl", params={"as_of": "2024-06-15"})
    assert r.status_code == 200
    body = r.json()
    assert body["series"]["series_id"] == "CPIAUCSL" and body["data_origin"] == "fixture:synthetic"
    assert body["effective_vintage_date"] == "2024-06-14"


@pytest.mark.parametrize(
    "payload", [{}, {"ticker": "MSFT", "quarters": 50}, {"ticker": "M$FT"}, {"question": "x" * 2001}]
)
def test_research_request_validation(client, payload):
    assert client.post("/research/company", json=payload).status_code == 422


def test_research_post_and_get(client):
    r = client.post("/research/company", json={"ticker": "MSFT", "quarters": 4, "use_llm": False})
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "completed" and run["report"]["verification"]["status"] != "failed"
    again = client.get(f"/research/{run['run_id']}")
    assert again.status_code == 200 and again.json()["report"]["run_id"] == run["run_id"]
    assert [s["step"] for s in run["steps"]][-1] == "persist"


def test_research_failure_is_recorded_not_masked(client):
    r = client.post("/research/company", json={"question": "Tell me about something unrelated."})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "failed" and "Could not identify" in body["error"] and body["report"] is None
