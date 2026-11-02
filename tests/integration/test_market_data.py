"""v0.3-A market data end to end, offline.

Price data: SYNTHETIC Alpaca-format fixtures (tests/fixtures/alpaca, MANIFEST kind=synthetic).
Treasury yields: REAL ALFRED DGS2/DGS10 vintages. Passing here does NOT verify live Alpaca.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from finance_agent.market.adjust import reconcile
from finance_agent.market.ingest import MarketDataIngestor
from finance_agent.market.schemas import AdjustmentBasis, QualityCheck
from finance_agent.market.service import MarketDataService
from finance_agent.market.store import MarketStore
from finance_agent.timeutil import ET
from tests.conftest import MARKET_END, MARKET_START


def svc(ctx) -> MarketDataService:
    return MarketDataService(ctx.db)


# ------------------------------------------------------------------ ingestion


def test_offline_ingestion_counts_and_provenance(market_ctx):
    run = market_ctx.market_run
    assert run.status == "completed" and not run.errors
    assert run.rows_received == 17 * 711 - 1  # 711 sessions; IWM 2025-03-14 deliberately absent
    assert run.versions_inserted == run.rows_received and run.duplicates == 0
    assert run.data_origin == "fixture:synthetic"
    bar = MarketStore(market_ctx.db).bar_versions("MSFT", date(2025, 6, 2))[0]
    assert bar.snapshot_sha256 and len(bar.snapshot_sha256) == 64 and bar.provider == "alpaca"
    assert bar.retrieved_at.isoformat() == "2026-10-30T18:00:00+00:00"  # MANIFEST recording time
    assert bar.available_at == datetime(2025, 6, 3, 0, 0, tzinfo=ET)  # D+1 00:00 New York
    assert str(bar.availability_precision) == "DATE_ONLY_CONSERVATIVE"
    # Raw provider response is preserved as an immutable, hash-verified snapshot.
    content = market_ctx.snapshots.read_blob(bar.snapshot_sha256)
    assert b'"MSFT"' in content


def test_reingestion_is_idempotent(market_ctx):
    run = MarketDataIngestor(market_ctx).run(["MSFT", "SPY"], MARKET_START, MARKET_END)
    assert run.versions_inserted == 0 and run.unchanged > 0 and run.corrections == 0
    assert run.cache_hits >= 2  # verified snapshots are reused, not re-downloaded


def test_expected_quality_findings_and_nothing_else(market_ctx):
    issues = MarketStore(market_ctx.db).issues(limit=5000)
    by = {(i.check, i.instrument_id, i.session_date) for i in issues}
    assert (QualityCheck.MISSING_SESSION, "IWM", date(2025, 3, 14)) in by
    assert (QualityCheck.EXTREME_RETURN, "XLE", date(2025, 8, 14)) in by
    assert (QualityCheck.CORPORATE_ACTION_DISCONTINUITY, "NVDA", date(2024, 6, 10)) in by
    provisional = {i.instrument_id for i in issues if i.check == QualityCheck.PROVISIONAL_BAR}
    assert len(provisional) == 17
    unexpected = [
        i for i in issues
        if i.check not in {QualityCheck.MISSING_SESSION, QualityCheck.EXTREME_RETURN,
                           QualityCheck.CORPORATE_ACTION_DISCONTINUITY, QualityCheck.PROVISIONAL_BAR}
    ]  # fmt: skip
    assert unexpected == []


# ------------------------------------------------------------------ adjustment


def test_split_adjusted_prices_and_reconciliation_with_provider(market_ctx):
    s = svc(market_ctx)
    raw = s.prices("NVDA", date(2024, 6, 3), date(2024, 6, 14), basis=AdjustmentBasis.RAW, mode="research")
    adj = s.prices(
        "NVDA", date(2024, 6, 3), date(2024, 6, 14), basis=AdjustmentBasis.SPLIT_ADJUSTED, mode="research"
    )
    d = date(2024, 6, 7)
    assert adj.closes[d] == pytest.approx(raw.closes[d] / 10)
    assert adj.closes[date(2024, 6, 10)] == raw.closes[date(2024, 6, 10)]
    provider = MarketStore(market_ctx.db).bars("NVDA", adjustment=str(AdjustmentBasis.PROVIDER_SPLIT))
    full = s.prices(
        "NVDA", MARKET_START, date(2026, 10, 29), basis=AdjustmentBasis.SPLIT_ADJUSTED, mode="research"
    )
    rec = reconcile(full.closes, {k: v.close for k, v in provider.items()}, rel_tol=1e-4)
    assert rec["within_tolerance"] and rec["sessions_compared"] > 700


def test_total_return_differs_from_split_adjusted_only_by_dividends(market_ctx):
    s = svc(market_ctx)
    tr = s.prices(
        "SPY", date(2025, 1, 2), date(2026, 10, 29), basis=AdjustmentBasis.TOTAL_RETURN, mode="research"
    )
    sp = s.prices(
        "SPY", date(2025, 1, 2), date(2026, 10, 29), basis=AdjustmentBasis.SPLIT_ADJUSTED, mode="research"
    )
    divs = [f for f in tr.factors.factors if f.basis == AdjustmentBasis.TOTAL_RETURN]
    assert divs and all(f.reference_close and 0.98 < f.price_factor < 1 for f in divs)
    # Backward adjustment: sessions after the last ex-date are unadjusted; earlier ones scaled down.
    assert tr.closes[date(2026, 10, 29)] == sp.closes[date(2026, 10, 29)]
    assert tr.closes[date(2025, 1, 2)] < sp.closes[date(2025, 1, 2)]
    assert tr.contract()["retrospectively_adjusted"] is True
    # Between ex-dates the ratio is the product of all later dividend factors (hand check).
    d0 = date(2025, 1, 2)
    later = [f.price_factor for f in divs if f.ex_date > d0]
    expected = 1.0
    for f in later:
        expected *= f
    assert tr.closes[d0] / sp.closes[d0] == pytest.approx(expected)


def test_raw_returns_refuse_to_span_a_split(market_ctx):
    res = svc(market_ctx).returns("NVDA", date(2024, 6, 3), date(2024, 6, 14), basis=AdjustmentBasis.RAW)
    split_day = next(r for r in res["returns"] if r["session_date"] == date(2024, 6, 10))
    assert split_day["value"] is None and "split" in split_day["note"]
    assert res["max_drawdown"] is None and res["cumulative_return"] is None
    ok = svc(market_ctx).returns(
        "NVDA", date(2024, 6, 3), date(2024, 6, 14), basis=AdjustmentBasis.SPLIT_ADJUSTED
    )
    assert all(r["value"] is not None for r in ok["returns"]) and ok["cumulative_return"] is not None


def test_correlations_on_stated_basis(market_ctx):
    res = svc(market_ctx).correlations(["SPY", "QQQ", "TLT"], date(2025, 1, 2), date(2025, 12, 31),
                                       basis=AdjustmentBasis.TOTAL_RETURN)  # fmt: skip
    assert res["pairs"]["SPY|SPY"]["correlation"] == pytest.approx(1.0)
    assert res["pairs"]["SPY|QQQ"]["observations"] > 200


# ------------------------------------------------------------------ Treasury yields (REAL)


def test_yields_are_percent_rates_with_bp_changes_and_missing_preserved(market_ctx):
    from finance_agent.market.yields import yield_series

    stats = market_ctx.yield_stats
    assert stats["inserted"] > 1000 and stats["vintage"] == "2026-11-01"
    res = yield_series(MarketStore(market_ctx.db), "DGS10", date(2025, 10, 8), date(2025, 10, 16), None)
    obs = {o["observation_date"]: o for o in res["observations"]}
    assert obs[date(2025, 10, 13)]["value"] is None  # Columbus Day: no observation published (kept as null)
    assert obs[date(2025, 10, 10)]["data_origin"] == "fixture:real_snapshot"
    ch = {c["date"]: c for c in res["changes"]}
    a, b = obs[date(2025, 10, 10)]["value"], obs[date(2025, 10, 14)]["value"]
    assert ch[date(2025, 10, 14)]["change_bp"] == pytest.approx((b - a) * 100)
    assert ch[date(2025, 10, 14)]["previous_date"] == date(2025, 10, 10)
    assert "not ETF prices" in " ".join(res["notes"])


# ------------------------------------------------------------------ API


@pytest.fixture(scope="module")
def client(market_ctx):
    from finance_agent.interfaces.api import create_app

    with TestClient(create_app(market_ctx.settings, market_ctx)) as c:
        yield c


def test_api_reads(client):
    assert client.get("/market/instruments").json()["count"] == 17
    r = client.get("/market/prices/NVDA", params={"start": "2024-06-06", "end": "2024-06-11", "basis": "split_adjusted",
                                                   "mode": "research"})  # fmt: skip
    assert r.status_code == 200 and r.json()["contract"]["retrospectively_adjusted"] is True
    assert r.json()["factors"][0]["source_action_id"] == "SYN-SPLIT-NVDA-2024-06-10"
    r = client.get(
        "/market/returns/SPY", params={"start": "2025-01-02", "end": "2025-03-31", "basis": "total_return"}
    )
    assert r.status_code == 200 and r.json()["returns"]
    cal = client.get(
        "/market/calendar", params={"date": "2025-01-09", "event_time": "2026-10-28T16:05:00-04:00"}
    ).json()
    assert cal["is_session"] is False and cal["alignment"]["phase"] == "AFTER_HOURS"
    y = client.get("/market/yields/DGS2", params={"start": "2026-10-01", "end": "2026-10-29"}).json()
    assert y["observations"] and y["units"].startswith("percent")
    q = client.get("/market/quality", params={"ticker": "IWM", "severity": "WARNING"}).json()
    assert q["count"] >= 1 and q["summary"]["MISSING_SESSION"] == 1


@pytest.mark.parametrize(
    "path,params,code",
    [
        ("/market/returns/SPY", {"start": "2025-01-02", "end": "2025-02-01"}, 422),  # basis is required
        ("/market/returns/SPY", {"start": "2025-01-02", "end": "2025-02-01", "basis": "adjusted"}, 422),
        ("/market/prices/SPY", {"start": "2025-02-01", "end": "2025-01-01"}, 422),
        (
            "/market/prices/SPY",
            {"start": "2025-01-02", "end": "2025-02-01", "mode": "research", "as_of": "2025-01-10"},
            422,
        ),
        ("/market/prices/ZZZZ", {"start": "2025-01-02", "end": "2025-02-01"}, 404),
        ("/market/yields/DGS30", {"start": "2025-01-02", "end": "2025-02-01"}, 404),
        ("/market/calendar", {"event_time": "2026-10-28T16:05:00"}, 422),  # naive timestamp
    ],
)
def test_api_validation(client, path, params, code):
    assert client.get(path, params=params).status_code == code


def test_api_ingest_is_protected(market_ctx):
    from pydantic import SecretStr

    from finance_agent.interfaces.api import create_app

    body = {"tickers": ["MSFT"], "start": "2026-10-01", "end": "2026-10-30", "yields": False}
    with TestClient(create_app(market_ctx.settings, market_ctx)) as c:
        assert c.post("/market/ingest", json=body).status_code == 403  # disabled without a token
    s = market_ctx.settings.model_copy(update={"ingest_admin_token": SecretStr("tok")})
    old = market_ctx.settings
    market_ctx.settings = s
    try:
        with TestClient(create_app(s, market_ctx)) as c:
            assert c.post("/market/ingest", json=body).status_code == 401
            assert c.post("/market/ingest", json=body, headers={"X-Admin-Token": "bad"}).status_code == 401
            ok = c.post("/market/ingest", json=body, headers={"X-Admin-Token": "tok"})
            assert ok.status_code == 200 and ok.json()["run"]["versions_inserted"] == 0
    finally:
        market_ctx.settings = old


# ------------------------------------------------------------------ CLI


def test_cli_market_commands(tmp_path, monkeypatch):
    from finance_agent.config.settings import get_settings
    from finance_agent.interfaces.cli import app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'cli.db').as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    r = CliRunner()
    try:
        out = r.invoke(app, ["market", "ingest", "--ticker", "NVDA", "--ticker", "IWM", "--start", "2024-05-01",
                             "--end", "2025-04-30", "--offline", "--no-yields"])  # fmt: skip
        assert out.exit_code == 0, out.output
        assert "SYNTHETIC FIXTURE" in out.output and "status=completed" in out.output
        out = r.invoke(app, ["market", "prices", "NVDA", "--start", "2024-06-06", "--end", "2024-06-11",
                             "--basis", "split_adjusted", "--offline"])  # fmt: skip
        assert out.exit_code == 0 and "POINT-IN-TIME" in out.output
        out = r.invoke(app, ["market", "returns", "IWM", "--start", "2025-03-10", "--end", "2025-03-20",
                             "--basis", "total_return", "--offline"])  # fmt: skip
        assert out.exit_code == 0 and "not computed" in out.output and "2025-03-14" in out.output
        out = r.invoke(app, ["market", "quality", "--offline"])
        assert out.exit_code == 0 and "MISSING_SESSION" in out.output
        out = r.invoke(app, ["market", "calendar", "--date", "2026-11-26"])
        assert out.exit_code == 0 and "NOT a trading session" in out.output
        out = r.invoke(app, ["market", "instruments", "--offline"])
        assert out.exit_code == 0 and "XLU" in out.output
        assert r.invoke(app, ["market", "returns", "IWM", "--start", "2025-03-10", "--end", "2025-03-20",
                              "--basis", "adjusted"]).exit_code == 1  # fmt: skip
        assert (
            r.invoke(app, ["financials", "MSFT", "--quarters", "2", "--offline"]).exit_code == 0
        )  # v0.1 intact
    finally:
        get_settings.cache_clear()
