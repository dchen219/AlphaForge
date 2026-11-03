"""Live integration tests against real SEC / FRED / Anthropic endpoints.

Opt-in only:  RUN_LIVE_TESTS=1 SEC_USER_AGENT="Name email" uv run pytest -m live
FRED tests additionally need FRED_API_KEY; LLM tests need a funded ANTHROPIC_API_KEY.
They are skipped (and reported as skipped) otherwise.
"""

from __future__ import annotations

import os

import pytest

from finance_agent.schemas.research import ResearchRequest
from finance_agent.services import AppContext
from finance_agent.storage.db import Database
from finance_agent.workflows.company_research import CompanyResearchWorkflow
from tests.conftest import offline_settings

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("RUN_LIVE_TESTS") != "1", reason="set RUN_LIVE_TESTS=1 to run"),
]
UA = os.environ.get("LIVE_SEC_USER_AGENT") or os.environ.get("SEC_USER_AGENT_LIVE")


@pytest.fixture
def live_ctx(tmp_path):
    ua = UA or os.environ.get("SEC_USER_AGENT")
    if not ua:
        pytest.skip("SEC_USER_AGENT (or LIVE_SEC_USER_AGENT) required for live SEC tests")
    s = offline_settings(
        tmp_path,
        offline_mode=False,
        sec_user_agent=ua,
        fred_api_key=os.environ.get("LIVE_FRED_API_KEY") or None,
        anthropic_api_key=os.environ.get("LIVE_ANTHROPIC_API_KEY") or None,
        llm_enabled=bool(os.environ.get("LIVE_ANTHROPIC_API_KEY")),
    )
    db = Database(s.database_url)
    db.create_all()
    c = AppContext(s, db=db)
    yield c
    c.close()


@pytest.mark.parametrize(
    "ticker,cik",
    [
        ("MSFT", "0000789019"),
        ("AAPL", "0000320193"),
        ("AMZN", "0001018724"),
        ("GOOGL", "0001652044"),
        ("META", "0001326801"),
    ],
)
def test_live_sec_ticker_and_financials(live_ctx, ticker, cik):
    ov = live_ctx.company(ticker)
    assert ov.profile.cik == cik and ov.data_origin == "live"
    res = live_ctx.financials(ticker, quarters=8)
    fin = res.financials
    assert fin.data_origin == "live" and len(fin.focus()) == 8
    assert all(v is not None for v in fin.series["revenue"][-8:])


def test_live_research_run(live_ctx):
    state = CompanyResearchWorkflow(live_ctx).run(ResearchRequest(ticker="MSFT", quarters=8, use_llm=False))
    assert not state.get("error"), state.get("error")
    rep = state["report"]
    assert rep.data_origin == "live" and rep.verification.status != "failed"


def test_live_fred(live_ctx):
    if not live_ctx.settings.fred_api_key:
        pytest.skip(
            "LIVE_FRED_API_KEY not set; keyed FRED API unverified (keyless ALFRED is covered by the v0.2 live test)"
        )
    res = live_ctx.macro("CPIAUCSL", as_of=None)
    assert res.data_origin == "live" and res.observations


def test_live_llm_synthesis(live_ctx):
    if not live_ctx.llm.available:
        pytest.skip("LIVE_ANTHROPIC_API_KEY not set; live LLM synthesis unverified")
    rep = CompanyResearchWorkflow(live_ctx).run(ResearchRequest(ticker="MSFT", quarters=4))["report"]
    assert rep.llm.status == "generated", rep.llm.notes


def test_live_v02_event_sources_and_briefing(live_ctx):
    """Live SEC + Federal Reserve + BLS/BEA calendars + ALFRED (keyless) ingestion and briefing."""
    from datetime import date

    from finance_agent.events.briefing import BriefingRequest
    from finance_agent.events.service import EventIngestor
    from finance_agent.workflows.briefing import BriefingWorkflow

    rep = EventIngestor(live_ctx).run(["sec", "fomc", "macro"], start=date(2026, 10, 1), tickers=["MSFT"])
    assert rep.ok, rep.errors
    assert all(rep.per_source[s]["inserted"] > 0 for s in ("sec", "fomc", "macro"))
    br = BriefingWorkflow(live_ctx).run(BriefingRequest(date="2026-10-28", use_llm=False))["report"]
    assert br.data_origins == ["live"] and br.verification.status != "failed"


def test_live_v03a_treasury_yields_keyless(live_ctx):
    """Live keyless ALFRED DGS2/DGS10 ingestion with point-in-time availability."""
    from datetime import date, datetime, timedelta

    from finance_agent.market.store import MarketStore
    from finance_agent.market.yields import YieldIngestor
    from finance_agent.timeutil import ET

    start = date.today() - timedelta(days=30)
    stats = YieldIngestor(live_ctx).run(None, start, date.today())
    assert stats["inserted"] > 20
    rows = MarketStore(live_ctx.db).yields("DGS10", start)
    assert rows and all(r["data_origin"] == "live" for r in rows)
    now = datetime.now(ET)
    assert all(r["available_at"] <= now + timedelta(days=2) for r in rows)
    assert all(r["value"] is None or 0 < r["value"] < 20 for r in rows)  # percent, not a price


def test_live_v03a_alpaca_market_data(live_ctx):
    """Live Alpaca bars + corporate actions. Requires LIVE_ALPACA_API_KEY_ID / LIVE_ALPACA_API_SECRET_KEY."""
    from datetime import date, timedelta

    from pydantic import SecretStr

    from finance_agent.market.ingest import MarketDataIngestor

    kid, sec = os.environ.get("LIVE_ALPACA_API_KEY_ID"), os.environ.get("LIVE_ALPACA_API_SECRET_KEY")
    if not (kid and sec):
        pytest.skip("LIVE_ALPACA_API_KEY_ID/SECRET not set; live Alpaca market data unverified")
    live_ctx.settings = live_ctx.settings.model_copy(
        update={"alpaca_api_key_id": SecretStr(kid), "alpaca_api_secret_key": SecretStr(sec)}
    )
    end = date.today() - timedelta(days=3)
    run = MarketDataIngestor(live_ctx).run(["SPY", "NVDA"], end - timedelta(days=60), end)
    assert run.status != "failed", run.errors
    assert run.rows_received > 50 and run.data_origin == "live"


def test_live_v03b_yield_event_study_on_live_alfred(live_ctx, tmp_path):
    """Live ALFRED DGS2/DGS10 + REAL recorded FOMC event: a yield event study on live data.

    Events come from the recorded v0.2 fixtures (the live SEC/Fed feeds are not needed); yields are
    fetched live. Values are cross-checked against the recorded fixture vintage for the same day."""
    from datetime import date

    from finance_agent.event_studies.schemas import EventStudyRequest
    from finance_agent.event_studies.service import EventStudyService
    from finance_agent.events.service import EventIngestor
    from finance_agent.market.yields import YieldIngestor
    from tests.conftest import fixture_window

    off = offline_settings(tmp_path / "events")
    EventIngestor(AppContext(off, db=live_ctx.db)).run(as_of=fixture_window()[1], start=fixture_window()[0])
    YieldIngestor(live_ctx).run(None, date(2025, 1, 2), date.today())
    r = EventStudyService(live_ctx).run(
        EventStudyRequest(event_id="EV-e6a9f7badcb72f25", series_id="DGS10", price_basis="total_return")
    )
    assert r.status == "COMPLETED" and not r.synthetic and "live" in r.data_origin
    assert r.result["unit"] == "basis_points" and r.result["day0"] == "2025-09-17"
    w = {x["window"]: x for x in r.result["windows"]}
    # recorded fixture vintage gives +2 bp on day 0 and +7 bp over [0,+1]; H.15 values are rarely revised
    assert w["[0,0]"]["change_bp"] == 2.0 and w["[0,+1]"]["change_bp"] == 7.0
