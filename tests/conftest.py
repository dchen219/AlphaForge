from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

import pytest

from finance_agent.config.settings import DEFAULT_FIXTURES_DIR, Settings
from finance_agent.events.service import EventIngestor
from finance_agent.services import AppContext
from finance_agent.storage.db import Database

FIXTURES = DEFAULT_FIXTURES_DIR

# Never let a developer's real environment leak into tests.
for _var in (
    "FRED_API_KEY",
    "ANTHROPIC_API_KEY",
    "SEC_USER_AGENT",
    "DATABASE_URL",
    "OFFLINE_MODE",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "INGEST_ADMIN_TOKEN",
):
    os.environ.pop(_var, None)


def offline_settings(tmp_path: Path, **overrides) -> Settings:
    base = dict(
        offline_mode=True,
        database_url=f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
        data_dir=tmp_path / "data",
        fixtures_dir=FIXTURES,
        llm_enabled=False,
        anthropic_api_key=None,
        fred_api_key=None,
        sec_user_agent=None,
        log_json=False,
        _env_file=None,
    )
    base.update(overrides)
    return Settings(**base)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return offline_settings(tmp_path)


@pytest.fixture
def ctx(settings: Settings):
    db = Database(settings.database_url)
    db.create_all()
    c = AppContext(settings, db=db)
    yield c
    c.close()


def fixture_window() -> tuple[date, datetime]:
    """The exact window the real v0.2 fixtures were recorded with (offline replay must match)."""
    w = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))["event_window"]
    return date.fromisoformat(w["start"]), datetime.fromisoformat(w["as_of"])


@pytest.fixture(scope="module")
def events_ctx(tmp_path_factory):
    """Offline context with all v0.2 sources ingested once from REAL recorded snapshots."""
    tmp = tmp_path_factory.mktemp("events")
    s = offline_settings(tmp)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    start, as_of = fixture_window()
    report = EventIngestor(ctx).run(as_of=as_of, start=start)
    ctx.ingest_report = report
    yield ctx
    ctx.close()


@pytest.fixture(scope="module")
def market_ctx(tmp_path_factory):
    """Offline context with the SYNTHETIC Alpaca-format bars and REAL ALFRED yields ingested once."""
    from finance_agent.market.ingest import MarketDataIngestor
    from finance_agent.market.schemas import AdjustmentBasis
    from finance_agent.market.yields import YieldIngestor

    tmp = tmp_path_factory.mktemp("market")
    s = offline_settings(tmp)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    ctx.market_run = MarketDataIngestor(ctx).run(None, MARKET_START, MARKET_END)
    # Provider split-adjusted bars (reconciliation only) exist for NVDA.
    ctx.provider_split_run = MarketDataIngestor(ctx).run(
        ["NVDA"], MARKET_START, MARKET_END, bases=(AdjustmentBasis.PROVIDER_SPLIT,), include_actions=False
    )
    ctx.yield_stats = YieldIngestor(ctx).run(None, MARKET_START, MARKET_END)
    yield ctx
    ctx.close()


MARKET_START, MARKET_END = date(2024, 1, 2), date(2026, 10, 30)


STUDY_TICKERS = ["SPY", "QQQ", "IWM", "XLK", "XLE", "MSFT", "NVDA", "IEF"]


def build_study_ctx(tmp: Path) -> AppContext:
    """REAL v0.2 events + SYNTHETIC Alpaca-format bars (subset) + REAL ALFRED yields, one database."""
    from finance_agent.market.ingest import MarketDataIngestor
    from finance_agent.market.yields import YieldIngestor

    s = offline_settings(tmp)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    start, as_of = fixture_window()
    ctx.ingest_report = EventIngestor(ctx).run(as_of=as_of, start=start)
    ctx.market_run = MarketDataIngestor(ctx).run(STUDY_TICKERS, MARKET_START, MARKET_END)
    ctx.yield_stats = YieldIngestor(ctx).run(None, MARKET_START, MARKET_END)
    return ctx


@pytest.fixture(scope="module")
def study_ctx(tmp_path_factory):
    ctx = build_study_ctx(tmp_path_factory.mktemp("study"))
    yield ctx
    ctx.close()
