"""Historical replay of v0.2 events: no information may be used before it was available.

REAL recorded snapshots (CPI 2026-10-14, Employment 2026-10-02, FOMC, MSFT filings) plus
SYNTHETIC injected events for cases real data does not contain (marked below).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from finance_agent.events.briefing import BriefingRequest, build_entry, verify_entries
from finance_agent.events.schemas import (
    AvailabilityPrecision,
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    VerificationStatus,
)
from finance_agent.events.service import EventIngestor
from finance_agent.events.store import EventStore
from finance_agent.services import AppContext
from finance_agent.storage.db import Database
from finance_agent.timeutil import ET
from finance_agent.workflows.briefing import BriefingWorkflow
from tests.conftest import fixture_window, offline_settings

CPI_KEY = "MACRO|CPIAUCSL|2026-09|FIRST_RELEASE"
CPI_TIME = datetime(2026, 10, 14, 8, 30, tzinfo=ET)  # official scheduled time, date confirmed by ALFRED


def visible(ctx, key: str, as_of: datetime) -> bool:
    return any(e.canonical_event_key == key for e in EventStore(ctx.db).query(as_of=as_of, limit=10000))


# ---------------------------------------------------------------- release timing boundaries


def test_release_before_at_and_after_its_timestamp(events_ctx):
    assert not visible(events_ctx, CPI_KEY, CPI_TIME - timedelta(seconds=1))
    assert visible(events_ctx, CPI_KEY, CPI_TIME)  # exactly at the timestamp: available
    assert visible(events_ctx, CPI_KEY, CPI_TIME + timedelta(hours=1))


def test_timezone_boundaries_expressed_in_utc(events_ctx):
    assert not visible(events_ctx, CPI_KEY, datetime(2026, 10, 14, 12, 29, 59, tzinfo=UTC))
    assert visible(events_ctx, CPI_KEY, datetime(2026, 10, 14, 12, 30, tzinfo=UTC))


def test_daylight_saving_offsets_on_real_releases(events_ctx):
    store = EventStore(events_ctx.db)
    winter = store.by_key("MACRO|CPIAUCSL|2025-12|FIRST_RELEASE")  # released Jan 2026 (EST)
    summer = store.by_key(CPI_KEY)  # released Oct 2026 (EDT)
    assert winter.available_at.astimezone(ET).hour == 8 and winter.available_at.astimezone(UTC).hour == 13
    assert summer.available_at.astimezone(ET).hour == 8 and summer.available_at.astimezone(UTC).hour == 12


def test_observation_period_is_not_publication_time(events_ctx):
    ev = EventStore(events_ctx.db).by_key(CPI_KEY)
    end_of_september = datetime(2026, 9, 30, 23, 59, tzinfo=ET)
    assert ev.observation_period == "2026-09" and not visible(events_ctx, CPI_KEY, end_of_september)


def test_revision_invisible_before_its_vintage_and_first_release_preserved(events_ctx):
    first_key, rev_key = "MACRO|PAYEMS|2026-08|FIRST_RELEASE", "MACRO|PAYEMS|2026-08|REVISION|2026-10-02"
    before = datetime(2026, 10, 2, 8, 29, tzinfo=ET)
    after = datetime(2026, 10, 2, 8, 31, tzinfo=ET)
    assert visible(events_ctx, first_key, before) and not visible(events_ctx, rev_key, before)
    assert visible(events_ctx, first_key, after) and visible(events_ctx, rev_key, after)
    store = EventStore(events_ctx.db)
    assert store.latest_version("MACRO|PAYEMS|2026-08|", as_of=before).canonical_event_key == first_key
    assert store.latest_version("MACRO|PAYEMS|2026-08|", as_of=after).canonical_event_key == rev_key


def test_post_cutoff_sec_filing_excluded(events_ctx):
    cut = datetime(2026, 10, 28, 20, 5, tzinfo=UTC)  # between 8-K (20:04:44Z) and 10-Q (20:07:39Z)
    ids = {
        e.source_document_id for e in EventStore(events_ctx.db).query(as_of=cut, ticker="MSFT", limit=10000)
    }
    assert "0001193125-26-436122" in ids and "0001193125-26-436133" not in ids


def test_fomc_statement_exact_time_boundary(events_ctx):
    t = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)
    assert not visible(events_ctx, "FOMC|DECISION|2026-09-16", t - timedelta(seconds=1))
    assert visible(events_ctx, "FOMC|DECISION|2026-09-16", t)


def test_strict_policy_option_delays_macro_to_next_day(tmp_path):
    s = offline_settings(tmp_path, macro_use_confirmed_scheduled_time=False)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    start, as_of = fixture_window()
    EventIngestor(ctx).run(["macro"], as_of=as_of, start=start)
    ev = EventStore(db).by_key(CPI_KEY)
    assert ev.availability_precision == AvailabilityPrecision.DATE_ONLY_CONSERVATIVE
    assert ev.available_at == datetime(2026, 10, 15, 0, 0, tzinfo=ET)


# ---------------------------------------------------------------- briefings


def test_historical_briefing_contains_only_pre_cutoff_events(events_ctx):
    as_of = datetime(2026, 10, 14, 9, 0, tzinfo=ET)
    rep = BriefingWorkflow(events_ctx).run(BriefingRequest(as_of=as_of, window_hours=72, use_llm=False))[
        "report"
    ]
    store = EventStore(events_ctx.db)
    for e in rep.macro_entries + rep.corporate_entries:
        for eid in e.event_ids:
            assert store.get(eid).available_at <= as_of
    assert any(
        e.primary_event_id == store.by_key(CPI_KEY).event_id or store.by_key(CPI_KEY).event_id in e.event_ids
        for e in rep.macro_entries
    )
    early = BriefingWorkflow(events_ctx).run(
        BriefingRequest(as_of=CPI_TIME - timedelta(minutes=1), window_hours=72, use_llm=False)
    )["report"]
    assert all(store.by_key(CPI_KEY).event_id not in e.event_ids for e in early.macro_entries)


def test_briefing_replay_is_deterministic(events_ctx):
    req = BriefingRequest(date="2026-10-28", use_llm=False)
    a = BriefingWorkflow(events_ctx).run(req)["report"]
    b = BriefingWorkflow(events_ctx).run(req)["report"]

    def strip(r):
        return [
            (e.cluster_id, e.event_ids, e.importance_score, e.what_was_reported)
            for e in r.macro_entries + r.corporate_entries
        ]

    assert strip(a) == strip(b) and a.executive_summary == b.executive_summary


def test_scheduled_but_unconfirmed_release_is_never_reported_as_occurred(events_ctx):
    """SYNTHETIC calendar entry inside the window with no confirming publication."""
    when = datetime(2026, 10, 28, 10, 0, tzinfo=ET)
    sched = FinancialEvent(
        canonical_event_key="SCHED|SYNTHETIC_TEST|2026-10-28",
        event_type=EventCategory.MACRO_RELEASE,
        event_subtype=EventSubtype.RELEASE_SCHEDULED,
        event_status=EventStatus.SCHEDULED,
        title="Scheduled: SYNTHETIC test release",
        source_name="SYNTHETIC calendar",
        scheduled_for=when,
        verification_status=VerificationStatus.SCHEDULE_ONLY,
        data_origin="fixture:synthetic",
    )
    EventStore(events_ctx.db).upsert(sched)
    rep = BriefingWorkflow(events_ctx).run(BriefingRequest(date="2026-10-28", use_llm=False))["report"]
    assert all(sched.event_id not in e.event_ids for e in rep.macro_entries + rep.corporate_entries)
    assert any("SYNTHETIC test release" in u and "not confirmed" in u for u in rep.uncertainty)
    check = next(c for c in rep.verification.checks if c.name == "scheduled_vs_published")
    assert check.status == "warning"


def test_leakage_guard_detects_post_cutoff_event(events_ctx):
    """NEGATIVE: an entry citing an event available after the cutoff must fail verification."""
    store = EventStore(events_ctx.db)
    ev = store.by_key(CPI_KEY)
    entry = build_entry(1, "macro", "X", [ev], {ev.event_id: [{"source_url": ev.source_url}]})
    checks = {
        c.name: c
        for c in verify_entries(
            [entry], {ev.event_id: ev}, CPI_TIME - timedelta(minutes=1), CPI_TIME - timedelta(days=1)
        )
    }
    assert checks["as_of_compliance"].status == "failed"
    ok = {
        c.name: c for c in verify_entries([entry], {ev.event_id: ev}, CPI_TIME, CPI_TIME - timedelta(days=1))
    }
    assert ok["as_of_compliance"].status == "passed" and ok["numeric_claims"].status == "passed"


def test_scheduled_event_presented_as_occurred_fails_verification(events_ctx):
    """NEGATIVE: the verifier rejects a SCHEDULED event inside an occurred-events section."""
    sched = EventStore(events_ctx.db).by_key("FOMC|MEETING|2026-12-09")
    entry = build_entry(
        1, "macro", "X", [sched], {sched.event_id: [{"source_url": "https://www.federalreserve.gov"}]}
    )
    checks = {
        c.name: c
        for c in verify_entries(
            [entry],
            {sched.event_id: sched},
            datetime(2026, 12, 31, tzinfo=UTC),
            datetime(2026, 12, 1, tzinfo=UTC),
        )
    }
    assert checks["occurrence_status"].status == "failed"


def test_tampered_number_fails_briefing_verification(events_ctx):
    """NEGATIVE: a figure not supported by the stored observation is caught."""
    ev = EventStore(events_ctx.db).by_key(CPI_KEY)
    entry = build_entry(1, "macro", "X", [ev], {ev.event_id: [{"source_url": ev.source_url}]})
    entry.what_was_reported = entry.what_was_reported.replace("+0.65%", "+0.95%")
    checks = {
        c.name: c for c in verify_entries([entry], {ev.event_id: ev}, CPI_TIME, CPI_TIME - timedelta(days=1))
    }
    assert checks["numeric_claims"].status == "failed"


@pytest.mark.parametrize("as_of", ["2026-10-01T23:59:59-04:00", "2026-10-14T08:29:59-04:00"])
def test_no_event_available_after_cutoff_is_ever_returned(events_ctx, as_of):
    cut = datetime.fromisoformat(as_of)
    for e in EventStore(events_ctx.db).query(as_of=cut, limit=100000):
        assert e.available_at <= cut
