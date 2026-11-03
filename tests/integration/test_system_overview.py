"""GET /system/overview: source/credential status as booleans only — never credential values."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from finance_agent.interfaces.api import create_app
from finance_agent.services import AppContext
from finance_agent.storage.db import Database
from tests.conftest import offline_settings

SECRETS = {
    "anthropic_api_key": "sk-ant-TEST-SECRET-0001",
    "fred_api_key": "fredsecret0002",
    "alpaca_api_key_id": "PKTESTSECRET0003",
    "alpaca_api_secret_key": "alpacasecretvalue0004",
    "ingest_admin_token": "admintokensecret0005",
}


def _client(tmp_path, **overrides) -> tuple[TestClient, AppContext]:
    s = offline_settings(tmp_path, **overrides)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    return TestClient(create_app(s, ctx)), ctx


def test_overview_without_credentials_reports_missing(tmp_path):
    client, ctx = _client(tmp_path)
    with client:
        body = client.get("/system/overview").json()
    ctx.close()
    src = {s["key"]: s for s in body["sources"]}
    assert set(src) == {"sec", "fred", "official_events", "treasury_yields", "alpaca", "anthropic"}
    assert [c["configured"] for c in src["alpaca"]["credentials"]] == [False, False]
    assert src["alpaca"]["live_ready"] is False and src["alpaca"]["offline_origin"] == "fixture:synthetic"
    assert src["sec"]["offline_origin"] == "fixture:real_snapshot"
    assert body["http_ingestion_enabled"] is False
    assert body["event_store"] == [] and body["market_runs"] == [] and body["research_runs"] == {}
    assert body["event_ingestion_runs_persisted"] is False


def test_overview_and_health_never_return_secret_values(tmp_path):
    client, ctx = _client(tmp_path, sec_user_agent="Test Name test@example.com", **SECRETS)
    with client:
        texts = [client.get(p).text for p in ("/system/overview", "/health", "/openapi.json")]
        body = client.get("/system/overview").json()
    ctx.close()
    for text in texts:
        for secret in SECRETS.values():
            assert secret not in text
        assert "test@example.com" not in text  # the SEC contact is configuration, not displayed
    src = {s["key"]: s for s in body["sources"]}
    assert all(c["configured"] for c in src["alpaca"]["credentials"]) and src["alpaca"]["live_ready"]
    assert src["anthropic"]["credentials"][0]["configured"] is True
    assert body["http_ingestion_enabled"] is True


def test_overview_summarizes_market_runs(market_ctx):
    with TestClient(create_app(market_ctx.settings, market_ctx)) as client:
        body = client.get("/system/overview").json()
    assert body["market_instruments"] > 0
    assert body["market_runs"] and {r["data_origin"] for r in body["market_runs"]} == {"fixture:synthetic"}


def test_ui_launcher_binds_loopback_only(monkeypatch):
    pytest.importorskip("streamlit")
    from finance_agent.ui.__main__ import streamlit_argv

    argv = streamlit_argv("8600")
    assert argv[argv.index("--server.address") + 1] == "127.0.0.1"
    assert argv[argv.index("--server.port") + 1] == "8600"
    for bad in ("80", "abc", "70000"):
        with pytest.raises(SystemExit):
            streamlit_argv(bad)
