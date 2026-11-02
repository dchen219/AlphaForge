"""Regressions for defects found in the v0.2 release-readiness audit (2026-11-02).

1. Historical briefings showed release-calendar entries from calendar snapshots retrieved AFTER the
   cutoff (calendars are not versioned): 291 schedule entries leaked into 488 replayed briefings.
2. An earnings 8-K and a 10-Q accepted shortly BEFORE it were two clusters (counted twice).
REAL recorded snapshots unless marked SYNTHETIC.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from finance_agent.events.briefing import BriefingRequest
from finance_agent.events.schemas import (
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    VerificationStatus,
)
from finance_agent.events.sources.sec_events import FilingRecord, SecEventBuilder
from finance_agent.events.store import EventStore, SourceLink
from finance_agent.storage.snapshots import SnapshotRef
from finance_agent.timeutil import ET
from finance_agent.workflows.briefing import BriefingWorkflow
from tests.conftest import FIXTURES

RECORDED = datetime.fromisoformat(
    json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))["groups"]["bls/"]["retrieved_at"]
)  # 2026-11-02T02:22:54Z = 2026-11-01 22:22 ET


def run(ctx, **kw):
    return BriefingWorkflow(ctx).run(BriefingRequest(use_llm=False, **kw))["report"]


def test_schedule_sources_carry_real_recording_time_offline(events_ctx):
    store = EventStore(events_ctx.db)
    sched = store.by_key("SCHED|BLS_CPI|2026-11-10")
    assert sched is not None and sched.event_status == EventStatus.SCHEDULED
    assert store.schedule_known_at([sched.event_id])[sched.event_id] == RECORDED


def test_historical_briefing_withholds_calendar_observed_after_cutoff(events_ctx):
    # 2026-11-06 Employment Situation is within 7 days of this cutoff, but the calendar listing it
    # was only retrieved on 2026-11-01: showing it would be look-ahead.
    rep = run(events_ctx, as_of=datetime(2026, 10, 31, 12, 0, tzinfo=ET))
    assert rep.upcoming == []
    assert any("withheld" in u and "look-ahead" in u for u in rep.uncertainty)
    assert "Upcoming scheduled releases" not in rep.markdown


def test_briefing_after_calendar_retrieval_still_lists_schedule(events_ctx):
    rep = run(events_ctx, as_of=RECORDED + timedelta(minutes=1))
    assert any("Employment Situation" in u["title"] for u in rep.upcoming)
    assert all(u["status"] == "SCHEDULED (not occurred)" for u in rep.upcoming)


def test_schedule_entry_without_observation_time_is_withheld(events_ctx):
    """SYNTHETIC calendar entry with no retrieved calendar snapshot: knowledge time unknown."""
    when = datetime(2026, 10, 27, 10, 0, tzinfo=ET)
    sched = FinancialEvent(
        canonical_event_key="SCHED|SYNTHETIC_NO_SNAPSHOT|2026-10-27",
        event_type=EventCategory.MACRO_RELEASE,
        event_subtype=EventSubtype.RELEASE_SCHEDULED,
        event_status=EventStatus.SCHEDULED,
        title="Scheduled: SYNTHETIC unobserved release",
        source_name="SYNTHETIC calendar",
        scheduled_for=when,
        verification_status=VerificationStatus.SCHEDULE_ONLY,
        data_origin="fixture:synthetic",
    )
    EventStore(events_ctx.db).upsert(sched, [SourceLink("synthetic:no-time", "SYNTHETIC", "schedule")])
    rep = run(events_ctx, date="2026-10-27")
    assert not any("SYNTHETIC unobserved" in u for u in rep.uncertainty)
    assert any("withheld" in u for u in rep.uncertainty)


# ---------------------------------------------------------------- SEC clustering (SYNTHETIC)

REF = SnapshotRef(
    1, "b" * 64, datetime(2026, 11, 1, tzinfo=UTC), "fixture:synthetic", "https://x.test", False
)


def filing(acc: str, form: str, accepted: datetime, items: tuple[str, ...] = ()) -> FilingRecord:
    return FilingRecord(
        "0000000002", acc, form, accepted.date(), accepted.date(), accepted, items, None, None
    )


TENQ = filing("0000000002-26-000010", "10-Q", datetime(2026, 10, 28, 20, 1, tzinfo=UTC))
EIGHTK = filing("0000000002-26-000011", "8-K", datetime(2026, 10, 28, 20, 3, tzinfo=UTC), ("2.02", "9.01"))


def test_10q_accepted_before_earnings_8k_is_one_cluster():
    evs = {
        e.source_document_id: e for e, _ in SecEventBuilder("SYN", "Syn").filing_events([TENQ, EIGHTK], REF)
    }
    q, k = evs[TENQ.accession], evs[EIGHTK.accession]
    assert q.event_cluster_id == k.event_cluster_id == f"CL-EARN-0000000002-{TENQ.accession}"
    assert q.related_event_id == k.event_id  # the earnings release is the related primary event


def test_cluster_id_never_names_a_later_filing():
    between = datetime(2026, 10, 28, 20, 2, tzinfo=UTC)
    (only,) = SecEventBuilder("SYN", "Syn").filing_events([TENQ, EIGHTK], REF, as_of=between)
    ev = only[0]
    assert ev.source_document_id == TENQ.accession
    assert EIGHTK.accession not in (ev.event_cluster_id or "") and ev.related_event_id is None


def test_unrelated_periodic_filing_is_not_clustered():
    far = filing("0000000002-26-000012", "10-Q", datetime(2026, 11, 5, 20, 0, tzinfo=UTC))
    evs = {
        e.source_document_id: e for e, _ in SecEventBuilder("SYN", "Syn").filing_events([EIGHTK, far], REF)
    }
    assert evs[far.accession].event_cluster_id is None


def test_point_in_time_query_masks_links_to_later_filings(tmp_path):
    """Integration review (PR #1): after full ingestion the 10-Q stores a link to, and an earnings
    cluster label implying, an 8-K accepted AFTER it. A historical query must not reveal either."""
    from finance_agent.storage.db import Database

    db = Database(f"sqlite:///{(tmp_path / 'pit.db').as_posix()}")
    db.create_all()
    store = EventStore(db)
    for ev, _ in SecEventBuilder("SYN", "Syn").filing_events([TENQ, EIGHTK], REF):  # ingested later
        store.upsert(ev)
    between = datetime(2026, 10, 28, 20, 2, tzinfo=UTC)
    (q,) = store.query(as_of=between, limit=10)
    assert q.source_document_id == TENQ.accession
    assert q.related_event_id is None and q.event_cluster_id is None
    after = {e.source_document_id: e for e in store.query(as_of=EIGHTK.accepted_at, limit=10)}
    assert after[TENQ.accession].event_cluster_id == after[EIGHTK.accession].event_cluster_id
    assert after[TENQ.accession].related_event_id == after[EIGHTK.accession].event_id
    assert store.get(q.event_id).related_event_id is not None  # stored state untouched
