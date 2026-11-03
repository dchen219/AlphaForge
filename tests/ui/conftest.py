"""A real AlphaForge API (offline fixtures, real recorded v0.2 snapshots) served on a loopback port.

Market data: SYNTHETIC Alpaca-format bars (NVDA, SPY) and REAL ALFRED Treasury yields for 2026.

UI tests drive Streamlit pages against this server over HTTP, the same path the console uses.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import date

import pytest
import uvicorn

from finance_agent.agents.llm import NullLLM
from finance_agent.events.service import EventIngestor
from finance_agent.interfaces.api import create_app
from finance_agent.market.ingest import MarketDataIngestor
from finance_agent.market.yields import YieldIngestor
from finance_agent.services import AppContext
from finance_agent.storage.db import Database
from tests.conftest import fixture_window, offline_settings

MARKET_START, MARKET_END = date(2026, 1, 2), date(2026, 10, 30)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def api_server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("ui_api")
    s = offline_settings(tmp)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    ctx.llm = NullLLM("no Anthropic credentials in tests")
    start, as_of = fixture_window()
    EventIngestor(ctx).run(as_of=as_of, start=start)
    MarketDataIngestor(ctx).run(["NVDA", "SPY"], MARKET_START, MARKET_END)
    YieldIngestor(ctx).run(None, MARKET_START, MARKET_END)
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(s, ctx), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline:  # pragma: no cover
            raise RuntimeError("API server did not start")
        time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "ctx": ctx, "as_of": as_of}
    server.should_exit = True
    thread.join(timeout=10)
    ctx.close()


@pytest.fixture
def ui_env(api_server, monkeypatch):
    """Point the console's offline backend at the test server; live backend points nowhere."""
    monkeypatch.setenv("ALPHAFORGE_API_URL", api_server["url"])
    monkeypatch.setenv("ALPHAFORGE_LIVE_API_URL", "http://127.0.0.1:9")  # discard port: unreachable
    return api_server
