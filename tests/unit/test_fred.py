"""FRED/ALFRED parsing and macro vintage selection (synthetic fixtures — not real data)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest

from finance_agent.errors import ConfigurationError, MalformedDataError, NotFoundError
from finance_agent.providers.fred import (
    VintageRow,
    normalize_series_id,
    parse_observations,
    parse_series_meta,
    select_vintage,
)
from tests.conftest import FIXTURES, offline_settings


def rows():
    """Jan-2024 CPI: first released 2024-02-13, revised 2024-03-12. Feb-2024 released 2024-03-12."""
    return [
        VintageRow(date(2024, 1, 1), 308.4, date(2024, 2, 13), date(2024, 3, 11)),
        VintageRow(date(2024, 1, 1), 308.7, date(2024, 3, 12), date(9999, 12, 31)),
        VintageRow(date(2024, 2, 1), 310.3, date(2024, 3, 12), date(9999, 12, 31)),
    ]


def et_noon(d: str) -> datetime:
    return datetime.fromisoformat(f"{d}T17:00:00+00:00")  # 12:00/13:00 New York


def test_parse_observations_handles_missing_values():
    out = parse_observations(
        {
            "observations": [
                {
                    "date": "2024-01-01",
                    "value": ".",
                    "realtime_start": "2024-02-01",
                    "realtime_end": "9999-12-31",
                },
                {
                    "date": "2024-02-01",
                    "value": "4.25",
                    "realtime_start": "2024-03-01",
                    "realtime_end": "9999-12-31",
                },
            ]
        }
    )
    assert out[0].value is None and out[1].value == 4.25
    with pytest.raises(MalformedDataError):
        parse_observations({"observations": [{"date": "2024-01-01"}]})
    with pytest.raises(MalformedDataError):
        parse_observations({})


def test_parse_series_meta():
    meta = parse_series_meta(json.loads((FIXTURES / "fred/series/GDP.json").read_text(encoding="utf-8")))
    assert meta.series_id == "GDP" and meta.title.startswith("[SYNTHETIC FIXTURE]")
    with pytest.raises(MalformedDataError):
        parse_series_meta({"seriess": []})


def test_latest_selection_without_as_of():
    sel = select_vintage(rows(), None)
    assert [(r.observation_date, r.value) for r in sel] == [
        (date(2024, 1, 1), 308.7),
        (date(2024, 2, 1), 310.3),
    ]


def test_as_of_before_first_release_excludes_observation():
    assert select_vintage(rows(), et_noon("2024-02-13")) == []  # same-day release not yet usable


def test_as_of_between_release_and_revision_uses_initial_value():
    sel = select_vintage(rows(), et_noon("2024-02-20"))
    assert [(r.observation_date, r.value) for r in sel] == [(date(2024, 1, 1), 308.4)]


def test_as_of_after_revision_uses_revised_value_and_new_observation():
    sel = select_vintage(rows(), et_noon("2024-03-13"))
    assert [(r.observation_date, r.value) for r in sel] == [
        (date(2024, 1, 1), 308.7),
        (date(2024, 2, 1), 310.3),
    ]
    # On the revision day itself the old vintage still applies (next-day policy).
    sel = select_vintage(rows(), et_noon("2024-03-12"))
    assert [(r.observation_date, r.value) for r in sel] == [(date(2024, 1, 1), 308.4)]


@pytest.mark.parametrize("bad", ["", "cpi-aucsl", "A" * 31, "../etc"])
def test_series_id_validation(bad):
    with pytest.raises(NotFoundError):
        normalize_series_id(bad)


def test_live_fred_requires_api_key(tmp_path):
    from finance_agent.services import AppContext
    from finance_agent.storage.db import Database

    s = offline_settings(tmp_path, offline_mode=False)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    with pytest.raises(ConfigurationError, match="FRED_API_KEY"):
        ctx.macro("GDP")
    ctx.close()


def test_provider_preserves_vintage_metadata_offline(ctx):
    res = ctx.macro("CPIAUCSL", as_of=datetime(2024, 6, 15, 17, tzinfo=UTC))
    assert res.data_origin == "fixture:synthetic" and res.effective_vintage_date == date(2024, 6, 14)
    for o in res.observations:
        assert o.realtime_start <= date(2024, 6, 14) <= o.realtime_end
        assert o.publication_time is None and o.publication_time_known is False
        assert o.available_at <= datetime(2024, 6, 15, 17, tzinfo=UTC)
        assert o.vintage_date == o.realtime_start and o.retrieved_at is not None
    assert res.observations[-1].observation_date < date(2024, 6, 1)


def test_missing_observation_is_flagged(ctx):
    res = ctx.macro("DGS2")
    missing = [o for o in res.observations if o.missing]
    assert missing and missing[0].value is None and any("missing" in w for w in res.warnings)
