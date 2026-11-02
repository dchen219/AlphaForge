"""v0.3-B look-ahead tests: point-in-time event studies must never use later information.

Prices are SYNTHETIC fixtures (recorded "retrieval" 2026-10-30 18:00Z); events REAL or SYNTHETIC
(labelled). Every test asserts both the excluded (future) and the included (known) side.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from finance_agent.event_studies.schemas import EventStudyRequest
from finance_agent.event_studies.service import EventStudyService
from finance_agent.events.schemas import (
    AvailabilityPrecision,
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    VerificationStatus,
)
from finance_agent.events.store import EventStore
from finance_agent.market.schemas import BarStatus, PriceBar
from finance_agent.market.store import MarketStore
from finance_agent.timeutil import ET
from tests.conftest import build_study_ctx

FOMC_SEP25 = "EV-e6a9f7badcb72f25"  # REAL, 2025-09-17 14:00 ET
FOMC_OCT25 = "EV-92f86e235b332761"  # REAL, 2025-10-29 14:00 ET
RECORDED = datetime(2026, 10, 30, 18, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def ctx(tmp_path_factory):
    c = build_study_ctx(tmp_path_factory.mktemp("study_replay"))
    yield c
    c.close()


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=ET)


def study(ctx, event_id, **kw):
    kw.setdefault("price_basis", "total_return")
    kw.setdefault("robustness", False)
    kw.setdefault("ticker", "QQQ")
    kw.setdefault("benchmark", "SPY")
    return EventStudyService(ctx).run(EventStudyRequest(event_id=event_id, **kw))


def pit(ctx, event_id, as_of, **kw):
    return study(ctx, event_id, data_mode="point_in_time", as_of=as_of, **kw)


def win(r, label):
    return next(w for w in r.result["windows"] if w["window"] == label)


def add_event(ctx, key, when, entities, available=None, category=EventCategory.OTHER_DISCLOSURE,
              subtype=EventSubtype.OTHER_MATERIAL_EVENT):  # fmt: skip
    ev = FinancialEvent(
        canonical_event_key=f"synthetic:{key}", event_type=category, event_subtype=subtype,
        event_status=EventStatus.PUBLISHED, title=f"SYNTHETIC {key}", source_name="SYNTHETIC test source",
        data_origin="fixture:synthetic", affected_entities=entities, published_at=when,
        available_at=available or when, availability_precision=AvailabilityPrecision.EXACT_TIMESTAMP,
        verification_status=VerificationStatus.UNVERIFIED,
    )  # fmt: skip
    EventStore(ctx.db).upsert(ev)
    return ev


def test_event_unknown_at_cutoff_is_ineligible(ctx):
    before = pit(ctx, FOMC_SEP25, ny(2025, 9, 17, 13, 59))
    assert before.status == "INELIGIBLE" and "not yet available" in before.result["status_reason"]
    assert before.result["windows"] == []


def test_windows_not_final_at_cutoff_are_withheld(ctx):
    # cutoff 2025-09-19 12:00 ET: bars for 09-17 and 09-18 are final (D+1 00:00), 09-19 is not
    r = pit(ctx, FOMC_SEP25, ny(2025, 9, 19, 12, 0))
    assert win(r, "[0,0]")["status"] == "VALID" and win(r, "[0,+1]")["status"] == "VALID"
    assert win(r, "[-1,+1]")["status"] == "VALID"
    assert win(r, "[0,+5]")["status"] == "NOT_AVAILABLE_AT_CUTOFF" and win(r, "[0,+5]")["car"] is None
    assert all(o["session"] <= "2025-09-18" or o["asset_return"] is None for o in r.result["observations"])


def test_point_in_time_never_silently_becomes_retrospective(ctx):
    """Bars were first retrieved on 2026-10-30, after this cutoff: availability rests on the backfill
    assumption, so the study is UNVERIFIED (never COMPLETED) and stays a point-in-time contract."""
    r = pit(ctx, FOMC_SEP25, ny(2025, 12, 31, 23, 59))
    assert r.status == "UNVERIFIED" and "backfill assumption" in r.result["status_reason"]
    m = r.result["data_manifest"]["target"]
    assert m["query_mode"] == "point_in_time" and m["retrospectively_adjusted"] is False
    assert m["point_in_time_verified"] is False and m["bars_retrieved_after_cutoff"] > 0
    ok = pit(ctx, FOMC_SEP25, datetime(2026, 11, 1, tzinfo=UTC))  # cutoff after the recorded retrieval
    assert ok.status == "COMPLETED" and ok.result["data_manifest"]["target"]["point_in_time_verified"] is True


def _bar(close: float, retrieved: datetime) -> PriceBar:
    """SYNTHETIC provider correction of the XLK 2025-10-29 close, observed at ``retrieved``."""
    return PriceBar(
        instrument_id="XLK", provider="alpaca", provider_symbol="XLK", session_date=date(2025, 10, 29),
        bar_timestamp=datetime(2025, 10, 29, 4, 0, tzinfo=UTC), open=close, high=close * 1.01,
        low=close * 0.99, close=close, volume=1_000_000.0, data_origin="fixture:synthetic",
        retrieved_at=retrieved, available_at=datetime(2025, 10, 30, 4, 0, tzinfo=UTC), bar_status=BarStatus.FINAL,
    )  # fmt: skip


def test_price_correction_after_cutoff_is_invisible_and_old_runs_are_preserved(ctx):
    kw = dict(ticker="XLK", windows=["0:0"])
    before_cut = datetime(2026, 11, 1, tzinfo=UTC)
    pit_before = pit(ctx, FOMC_OCT25, before_cut, **kw)
    research_before = study(ctx, FOMC_OCT25, **kw)
    car0 = win(research_before, "[0,0]")["car"]
    assert pit_before.status == "COMPLETED" and win(pit_before, "[0,0]")["car"] == pytest.approx(
        car0, abs=1e-15
    )

    store = MarketStore(ctx.db)
    orig = store.bars("XLK", start=date(2025, 10, 29), end=date(2025, 10, 29))[date(2025, 10, 29)]
    seen = datetime(2026, 11, 5, 15, 0, tzinfo=UTC)
    assert store.insert_bar(_bar(orig.close * 1.05, seen)) == "correction"

    pit_again = pit(ctx, FOMC_OCT25, before_cut, **kw)  # cutoff before the correction was observed
    assert pit_again.run_id == pit_before.run_id and pit_again.existing
    assert win(pit_again, "[0,0]")["car"] == pytest.approx(car0, abs=1e-15)
    pit_after = pit(ctx, FOMC_OCT25, datetime(2026, 11, 6, tzinfo=UTC), **kw)
    research_after = study(ctx, FOMC_OCT25, **kw)
    for r in (pit_after, research_after):
        assert win(r, "[0,0]")["car"] > car0 + 0.04  # +5 % close on day 0 (and a lower day +1 base)
        assert r.result["data_manifest"]["target"]["corrected_sessions"] == ["2025-10-29"]
    assert research_after.run_id != research_before.run_id  # new data -> new run, never an overwrite
    stored = EventStudyService(ctx).get(research_before.run_id)
    assert win(stored, "[0,0]")["car"] == pytest.approx(car0, abs=1e-15)


def test_future_corporate_action_is_not_used_before_it_is_known(ctx):
    ev = add_event(ctx, "nvda-march", ny(2024, 3, 28, 8, 0), ["NVDA"])
    kw = dict(ticker="NVDA", price_basis="split_adjusted", estimation_start=-55, estimation_end=-6,
              min_observations=40)  # fmt: skip
    early = pit(ctx, ev.event_id, ny(2024, 5, 15, 23, 59), **kw)
    retro = study(ctx, ev.event_id, **kw)
    f_early = early.result["data_manifest"]["target"]["adjustment_factors"]
    f_retro = retro.result["data_manifest"]["target"]["adjustment_factors"]
    assert all(f["ex_date"] < "2024-05-16" for f in f_early) and not any(
        f["ex_date"] == "2024-06-10" for f in f_early
    )
    assert any(f["ex_date"] == "2024-06-10" for f in f_retro)  # retrospective adjustment uses the later split
    assert retro.result["data_manifest"]["target"]["retrospectively_adjusted"] is True
    # a backward split factor after the window scales every price equally: returns are unchanged
    assert win(early, "[0,+5]")["car"] == pytest.approx(win(retro, "[0,+5]")["car"], abs=1e-12)


def test_confounder_published_in_window_but_known_later_is_hidden_point_in_time(ctx):
    base = add_event(ctx, "msft-pit", ny(2025, 9, 2, 8, 0), ["MSFT"])
    # published at +2 but only AVAILABLE a week later (after the point-in-time cutoff)
    add_event(ctx, "msft-late", ny(2025, 9, 4, 10, 0), ["MSFT"], available=ny(2025, 9, 11, 9, 0))
    kw = dict(ticker="MSFT")
    cutoff = ny(2025, 9, 10, 9, 0)  # all [0,+5] bars final (last session 2025-09-09)
    p = pit(ctx, base.event_id, cutoff, **kw)
    r = study(ctx, base.event_id, **kw)
    names = lambda res: [c["title"] for c in res.result["confounders"]["confounders"]]  # noqa: E731
    assert "SYNTHETIC msft-late" not in names(p)
    assert "SYNTHETIC msft-late" in names(r)
    assert p.result["confounders"]["point_in_time_as_of"] is not None


def test_synthetic_and_real_inputs_are_labelled_in_point_in_time_runs(ctx):
    eq = pit(ctx, FOMC_SEP25, datetime(2026, 11, 1, tzinfo=UTC))
    assert eq.synthetic and "SYNTHETIC DATA" in eq.report_markdown
    y = pit(
        ctx, FOMC_SEP25, datetime(2026, 11, 1, tzinfo=UTC), ticker=None, series_id="DGS10", benchmark=None
    )
    assert (
        not y.synthetic and y.data_origin == "fixture:real_snapshot" and "SYNTHETIC" not in y.report_markdown
    )
    # yields seen point-in-time only include vintages available at the cutoff
    early = pit(ctx, FOMC_SEP25, ny(2025, 9, 18, 12, 0), ticker=None, series_id="DGS10", benchmark=None)
    assert (
        early.status == "INSUFFICIENT_DATA"
    )  # day 0 (09-17) value is first published 09-18 -> available 09-19
