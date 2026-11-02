from __future__ import annotations

import os
from pathlib import Path

import pytest

from finance_agent.config.settings import DEFAULT_FIXTURES_DIR, Settings
from finance_agent.services import AppContext
from finance_agent.storage.db import Database

FIXTURES = DEFAULT_FIXTURES_DIR

# Never let a developer's real environment leak into tests.
for _var in ("FRED_API_KEY", "ANTHROPIC_API_KEY", "SEC_USER_AGENT", "DATABASE_URL", "OFFLINE_MODE"):
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
