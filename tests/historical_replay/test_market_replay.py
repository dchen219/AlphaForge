"""v0.3-A point-in-time integrity: nothing is used before it was available.

Prices: SYNTHETIC Alpaca-format fixtures. Yields and the availability-rule calibration: REAL
ALFRED vintages. Negative cases are deliberate look-ahead attempts that must fail.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from finance_agent.events.sources.macro import parse_alfred_csv
from finance_agent.market.ingest import MarketDataIngestor
from finance_agent.market.providers import ProviderBar
from finance_agent.market.schemas import AdjustmentBasis, BarStatus, QualityStatus
from finance_agent.market.service import MarketDataService
from finance_agent.market.store import MarketStore
from finance_agent.market.yields import first_vintage_rule
from finance_agent.services import AppContext
from finance_agent.storage.db import Database
from finance_agent.timeutil import ET
from tests.conftest import FIXTURES, offline_settings

SPLIT_EX = date(2024, 6, 10)
CORRECTED = date(2025, 6, 2)
CORRECTION_SEEN = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=ET)


@pytest.fixture(scope="module")
def replay(tmp_path_factory):
    """MSFT/NVDA/SPY/IWM ingested; then a SYNTHETIC provider correction to MSFT arrives later."""
    tmp = tmp_path_factory.mktemp("market_replay")
    s = offline_settings(tmp)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    MarketDataIngestor(ctx).run(["MSFT", "NVDA", "SPY", "IWM"], date(2024, 1, 2), date(2026, 10, 30))
    # Corrected fixture root: same recording, one MSFT close changed, observed on 2026-11-05.
    root = tmp / "fixtures_corrected"
    shutil.copytree(FIXTURES / "alpaca", root / "alpaca")
    manifest = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["groups"]["alpaca/"]["retrieved_at"] = CORRECTION_SEEN.isoformat()
    (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    path = root / "alpaca" / "bars" / "raw" / "MSFT.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for b in data["bars"]["MSFT"]:
        if b["t"].startswith(CORRECTED.isoformat()):
            ctx.original_close = b["c"]
            b["c"] = round(b["c"] + 1.0, 2)
            b["h"] = max(b["h"], b["c"])
    path.write_text(json.dumps(data), encoding="utf-8")
    ctx2 = AppContext(s.model_copy(update={"fixtures_dir": root}), db=db)
    ctx.correction_run = MarketDataIngestor(ctx2).run(
        ["MSFT"], date(2024, 1, 2), date(2026, 10, 30), refresh=True
    )
    yield ctx
    ctx2.close()
    ctx.close()


def svc(ctx) -> MarketDataService:
    return MarketDataService(ctx.db)


# ------------------------------------------------------------------ corrections


def test_correction_after_cutoff_is_invisible_before_it_arrived(replay):
    """NEGATIVE: a market-data correction arriving after the cutoff must not leak backwards."""
    assert replay.correction_run.corrections == 1
    store = MarketStore(replay.db)
    versions = store.bar_versions("MSFT", CORRECTED)
    assert [v.version for v in versions] == [1, 2]  # original retained, not overwritten
    assert versions[1].available_at == CORRECTION_SEEN
    assert str(versions[1].availability_precision) == "OBSERVED_AT_RETRIEVAL"
    before = store.bars("MSFT", start=CORRECTED, end=CORRECTED, as_of=CORRECTION_SEEN - timedelta(seconds=1))
    after = store.bars("MSFT", start=CORRECTED, end=CORRECTED, as_of=CORRECTION_SEEN)
    assert before[CORRECTED].close == replay.original_close
    assert after[CORRECTED].close == pytest.approx(replay.original_close + 1.0)


def test_provisional_bar_is_finalized_by_a_later_retrieval(replay):
    versions = MarketStore(replay.db).bar_versions("MSFT", date(2026, 10, 30))
    assert [str(v.bar_status) for v in versions] == ["PROVISIONAL", "FINAL"]


# ------------------------------------------------------------------ bar availability


def test_daily_bar_not_available_before_next_day_new_york_midnight(replay):
    store = MarketStore(replay.db)
    d = date(2025, 3, 13)
    assert d not in store.bars("SPY", start=d, end=d, as_of=ny(2025, 3, 13, 23, 59, 59))
    assert d in store.bars("SPY", start=d, end=d, as_of=ny(2025, 3, 14, 0, 0))


def test_provisional_current_day_bar_is_never_presented_as_final(replay):
    """NEGATIVE: the 2026-10-30 bar was retrieved at 14:00 ET during the session."""
    cut = datetime(2026, 10, 30, 19, 0, tzinfo=UTC)  # after retrieval, before D+1 00:00 New York
    ps = svc(replay).prices("SPY", date(2026, 10, 26), date(2026, 10, 30), as_of=cut)
    assert date(2026, 10, 30) not in ps.closes and date(2026, 10, 30) not in ps.sessions
    shown = svc(replay).prices(
        "SPY", date(2026, 10, 26), date(2026, 10, 30), as_of=cut, include_provisional=True
    )
    row = next(r for r in shown.rows if r["session_date"] == date(2026, 10, 30))
    assert row["bar_status"] == "PROVISIONAL" and date(2026, 10, 30) not in shown.closes
    assert any("PROVISIONAL" in n for n in shown.notes)
    rets = svc(replay).returns(
        "SPY", date(2026, 10, 26), date(2026, 10, 30), basis=AdjustmentBasis.RAW, as_of=cut
    )
    assert all(r["session_date"] != date(2026, 10, 30) for r in rets["returns"])


def test_missing_price_is_reported_not_replaced(replay):
    """NEGATIVE: the absent IWM 2025-03-14 bar is never filled from a neighbouring session."""
    ps = svc(replay).prices("IWM", date(2025, 3, 12), date(2025, 3, 18), mode="research")
    row = next(r for r in ps.rows if r["session_date"] == date(2025, 3, 14))
    assert "close" not in row and row["gap"] == "MISSING_DATA"
    assert date(2025, 3, 14) not in ps.closes


def test_holiday_bar_is_quarantined_and_excluded(replay):
    """NEGATIVE: a SYNTHETIC provider bar dated Christmas Day is not treated as a trading day."""
    from finance_agent.storage.snapshots import SnapshotRef

    ing = MarketDataIngestor(replay)
    src = MarketStore(replay.db).bar_versions("SPY", date(2025, 12, 24))[0]
    sref = SnapshotRef(
        src.snapshot_id, src.snapshot_sha256, src.retrieved_at, "fixture:synthetic", "https://x.test", False
    )
    pb = ProviderBar("SPY", datetime(2025, 12, 25, 5, 0, tzinfo=UTC), 1, 2, 0.5, 1.5, 10, None, None, sref)
    bar, issues = ing.normalize("SPY", pb, AdjustmentBasis.RAW)
    assert bar.quality_status == QualityStatus.QUARANTINED and issues
    ing.store.insert_bar(bar)
    assert date(2025, 12, 25) not in MarketStore(replay.db).bars(
        "SPY", start=date(2025, 12, 20), end=date(2025, 12, 31)
    )
    ps = svc(replay).prices("SPY", date(2025, 12, 22), date(2025, 12, 31), mode="research")
    assert date(2025, 12, 25) not in ps.sessions


# ------------------------------------------------------------------ corporate actions


def test_split_not_yet_effective_is_not_applied_point_in_time(replay):
    """NEGATIVE: a split adjustment based on a corporate action not yet public at the cutoff."""
    d = date(2024, 6, 7)
    pit = svc(replay).prices(
        "NVDA", date(2024, 6, 3), d, basis=AdjustmentBasis.SPLIT_ADJUSTED, as_of=ny(2024, 6, 8, 12)
    )
    research = svc(replay).prices(
        "NVDA", date(2024, 6, 3), d, basis=AdjustmentBasis.SPLIT_ADJUSTED, mode="research"
    )
    raw = svc(replay).prices("NVDA", date(2024, 6, 3), d, as_of=ny(2024, 6, 8, 12))
    assert pit.closes[d] == raw.closes[d]  # split unknown at the cutoff: no adjustment
    assert research.closes[d] == pytest.approx(raw.closes[d] / 10)  # retrospective view adjusts
    assert (
        pit.contract()["retrospectively_adjusted"] is False
        and research.contract()["retrospectively_adjusted"]
    )
    on_ex = svc(replay).prices(
        "NVDA", date(2024, 6, 3), d, basis=AdjustmentBasis.SPLIT_ADJUSTED, as_of=ny(2024, 6, 10, 9)
    )
    assert on_ex.closes[d] == pytest.approx(raw.closes[d] / 10)


def test_future_dividend_not_in_point_in_time_total_return(replay):
    cut = ny(2025, 3, 1)
    ps = svc(replay).prices(
        "SPY", date(2025, 1, 2), date(2025, 2, 28), basis=AdjustmentBasis.TOTAL_RETURN, as_of=cut
    )
    assert all(f.ex_date <= cut.date() for f in ps.factors.factors)
    research = svc(replay).prices(
        "SPY", date(2025, 1, 2), date(2025, 2, 28), basis=AdjustmentBasis.TOTAL_RETURN, mode="research"
    )
    assert any(f.ex_date > cut.date() for f in research.factors.factors)


def test_no_bar_available_after_cutoff_is_ever_returned(replay):
    store = MarketStore(replay.db)
    for cut in (ny(2024, 6, 10, 0, 0), ny(2025, 12, 31, 23, 0), CORRECTION_SEEN - timedelta(minutes=1)):
        for t in ("MSFT", "NVDA", "SPY", "IWM"):
            for b in store.bars(t, as_of=cut, include_provisional=True).values():
                assert b.available_at <= cut
                assert b.bar_status == BarStatus.FINAL or b.retrieved_at <= cut


# ------------------------------------------------------------------ Treasury yields (REAL vintages)


def _vintage(sid: str, d: date) -> dict:
    return parse_alfred_csv((FIXTURES / "alfred" / sid / f"{d.isoformat()}.csv").read_bytes(), sid)


@pytest.mark.parametrize("sid", ["DGS2", "DGS10"])
def test_yield_availability_rule_is_never_earlier_than_real_first_vintage(sid):
    """Calibration on REAL consecutive-day ALFRED vintages around holidays (incl. a Saturday holiday
    observed on Friday and executive-order closures). The rule may be late, never early."""
    windows = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))["yield_window"][
        "calibration_windows"
    ]
    checked = early = 0
    for a, b in windows:
        d = date.fromisoformat(a) + timedelta(days=1)
        while d <= date.fromisoformat(b):
            new = set(_vintage(sid, d)) - set(_vintage(sid, d - timedelta(days=1)))
            for obs in new:
                checked += 1
                early += first_vintage_rule(obs) < d
            d += timedelta(days=1)
    assert checked >= 30 and early == 0


def test_point_in_time_yields(tmp_path):
    from finance_agent.market.yields import YieldIngestor

    s = offline_settings(tmp_path)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    YieldIngestor(ctx).run(["DGS10"], date(2026, 10, 1), date(2026, 10, 31))
    store = MarketStore(db)
    obs = date(2026, 10, 28)  # real first vintage 2026-10-29 -> available 2026-10-30 00:00 New York
    seen = lambda cut: obs in {r["observation_date"] for r in store.yields("DGS10", as_of=cut)}  # noqa: E731
    assert not seen(ny(2026, 10, 29, 23, 59)) and seen(ny(2026, 10, 30, 0, 0))
    assert Path(FIXTURES / "alfred" / "DGS10" / "2026-10-29.csv").exists()
    assert obs in _vintage("DGS10", date(2026, 10, 29)) and obs not in _vintage("DGS10", date(2026, 10, 28))
    ctx.close()
