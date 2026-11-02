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
        pytest.skip("LIVE_FRED_API_KEY not set; live FRED/ALFRED unverified")
    res = live_ctx.macro("CPIAUCSL", as_of=None)
    assert res.data_origin == "live" and res.observations


def test_live_llm_synthesis(live_ctx):
    if not live_ctx.llm.available:
        pytest.skip("LIVE_ANTHROPIC_API_KEY not set; live LLM synthesis unverified")
    rep = CompanyResearchWorkflow(live_ctx).run(ResearchRequest(ticker="MSFT", quarters=4))["report"]
    assert rep.llm.status == "generated", rep.llm.notes
