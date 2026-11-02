"""Offline ingestion of REAL recorded official snapshots: SEC, FOMC, BLS/BEA via ALFRED."""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime

import httpx
import pytest
from sqlalchemy import func, select

from finance_agent.events.importance import METHOD_VERSION
from finance_agent.events.service import EventIngestor
from finance_agent.events.store import EventStore
from finance_agent.http import FixtureTransport
from finance_agent.services import AppContext
from finance_agent.storage.db import Database
from finance_agent.storage.models import EventSourceRecord, FinancialEventRecord
from finance_agent.timeutil import ET
from tests.conftest import FIXTURES, fixture_window, offline_settings


def q(ctx, **kw):
    return EventStore(ctx.db).query(limit=5000, **kw)


def test_all_sources_ingest_without_errors(events_ctx):
    rep = events_ctx.ingest_report
    assert rep.ok, rep.errors
    for src in ("sec", "fomc", "macro"):
        assert rep.per_source[src]["inserted"] > 0, src
        assert rep.per_source[src]["conflicts"] == []


def test_repeated_ingestion_is_idempotent(events_ctx):
    with events_ctx.db.session() as s:
        n_events = s.scalar(select(func.count()).select_from(FinancialEventRecord))
        n_sources = s.scalar(select(func.count()).select_from(EventSourceRecord))
    start, as_of = fixture_window()
    again = EventIngestor(events_ctx).run(as_of=as_of, start=start)
    assert again.ok
    for src, stats in again.per_source.items():
        assert stats["inserted"] == 0 and stats["sources_added"] == 0 and stats["conflicts"] == [], src
    with events_ctx.db.session() as s:
        assert s.scalar(select(func.count()).select_from(FinancialEventRecord)) == n_events
        assert s.scalar(select(func.count()).select_from(EventSourceRecord)) == n_sources


# ---------------------------------------------------------------- frozen real cases


def test_frozen_cpi_release_september_2026(events_ctx):
    """REAL: BLS CPI for Sep 2026, scheduled 2026-10-14 08:30 ET, confirmed by ALFRED vintages."""
    ev = EventStore(events_ctx.db).by_key("MACRO|CPIAUCSL|2026-09|FIRST_RELEASE")
    assert ev is not None and ev.data_origin == "fixture:real_snapshot"
    assert ev.available_at == datetime(2026, 10, 14, 8, 30, tzinfo=ET)
    assert ev.availability_precision == "SCHEDULED_TIME_CONFIRMED_DATE"
    assert ev.value == pytest.approx(0.6487, abs=1e-3) and ev.previous_value == pytest.approx(0.396, abs=1e-3)
    comps = {c.kind: c for c in ev.comparisons}
    assert comps["yoy_pct_change"].value == pytest.approx(3.717, abs=1e-3)
    assert ev.consensus_status == "unavailable" and ev.consensus_value is None
    assert ev.event_cluster_id == "CL-BLS_CPI-2026-10-14"
    core = EventStore(events_ctx.db).by_key("MACRO|CPILFESL|2026-09|FIRST_RELEASE")
    assert core.event_cluster_id == ev.event_cluster_id  # same release, one cluster
    roles = {r.role for r in EventStore(events_ctx.db).sources(ev.event_id)}
    assert {"vintage", "vintage_prior", "schedule"} <= roles


def test_frozen_employment_release_with_revisions(events_ctx):
    """REAL: Employment Situation 2026-10-02 — Sep payrolls +29k; July/Aug revised (new events)."""
    store = EventStore(events_ctx.db)
    nfp = store.by_key("MACRO|PAYEMS|2026-09|FIRST_RELEASE")
    assert nfp.value == pytest.approx(29.0) and nfp.unit == "THOUSANDS_OF_PERSONS"
    rev = store.by_key("MACRO|PAYEMS|2026-08|REVISION|2026-10-02")
    assert (
        rev.event_status == "REVISED"
        and rev.value == pytest.approx(133.0)
        and rev.previous_value == pytest.approx(162.0)
    )
    first = store.by_key("MACRO|PAYEMS|2026-08|FIRST_RELEASE")
    assert first is not None and first.value == pytest.approx(
        162.0
    )  # first release preserved, never overwritten
    assert rev.supersedes_event_id == first.event_id
    ur = store.by_key("MACRO|UNRATE|2026-09|FIRST_RELEASE")
    assert ur.value == pytest.approx(4.2) and ur.unit == "PERCENT"


def test_frozen_fomc_decisions(events_ctx):
    """REAL: Federal Reserve statements; decisions read from the statement's own sentence."""
    store = EventStore(events_ctx.db)
    sep = store.by_key("FOMC|DECISION|2026-09-16")
    assert sep.event_subtype == "RATE_HIKE" and sep.attributes["target_range_upper"] == 4.0
    assert sep.published_at == datetime(2026, 9, 16, 18, 0, tzinfo=UTC)  # RSS pubDate, exact
    assert sep.availability_precision == "EXACT_TIMESTAMP"
    assert {c.kind: c.value for c in sep.comparisons}["rate_change"] == pytest.approx(25.0)
    excerpt = store.sources(sep.event_id)[0].excerpt
    assert "decided to raise the target range" in excerpt
    meeting = store.by_key("FOMC|MEETING|2026-12-09")
    assert meeting.event_status == "SCHEDULED" and store.by_key("FOMC|DECISION|2026-12-09") is None


def test_frozen_sec_filings_msft(events_ctx):
    """REAL: MSFT earnings 8-K (item 2.02) and 10-Q on 2026-10-28, one economic cluster."""
    evs = q(
        events_ctx,
        ticker="MSFT",
        start=datetime(2026, 10, 28, tzinfo=ET),
        end=datetime(2026, 10, 29, tzinfo=ET),
    )
    by = {e.source_document_id: e for e in evs if e.event_subtype != "FINANCIAL_METRIC_CHANGE"}
    earn = by["0001193125-26-436122"]
    tenq = by["0001193125-26-436133"]
    assert earn.event_type == "CORPORATE_EARNINGS" and earn.attributes["items"] == ["2.02", "9.01"]
    assert earn.available_at == datetime(2026, 10, 28, 20, 4, 44, tzinfo=UTC)  # acceptance time (UTC)
    assert tenq.event_subtype == "FORM_10Q" and tenq.event_cluster_id == earn.event_cluster_id
    other = by["0001193125-26-436149"]
    assert other.attributes["items"] == ["8.01", "9.01"] and other.event_subtype == "OTHER_MATERIAL_EVENT"


def test_metric_change_event_uses_v01_calculations(events_ctx):
    ev = EventStore(events_ctx.db).by_key("SEC|0000789019|0001193125-26-436133|METRICS")
    assert ev is not None and ev.event_subtype == "FINANCIAL_METRIC_CHANGE"
    assert ev.observation_period == "FY2027 Q1"
    assert ev.value == pytest.approx(18.8, abs=0.05)  # MSFT revenue YoY (matches v0.1 report)
    assert ev.availability_precision == "DATE_ONLY_CONSERVATIVE"
    assert ev.available_at == datetime(2026, 10, 29, 0, 0, tzinfo=ET)


def test_scheduled_events_are_not_returned_as_occurred(events_ctx):
    occurred = q(events_ctx)
    assert occurred and all(e.event_status in {"PUBLISHED", "REVISED"} for e in occurred)
    cal = EventStore(events_ctx.db).calendar(
        start=datetime(2026, 11, 1, tzinfo=UTC), end=datetime(2026, 12, 31, tzinfo=UTC)
    )
    assert cal and all(e.event_status == "SCHEDULED" and e.value is None for e in cal)


def test_every_occurred_event_is_traceable(events_ctx):
    store = EventStore(events_ctx.db)
    for e in q(events_ctx):
        assert e.source_url and e.available_at and e.data_origin.startswith("fixture:real_snapshot")
        assert e.importance_components.get("method") == METHOD_VERSION
        assert store.sources(e.event_id), e.canonical_event_key


# ---------------------------------------------------------------- failure handling


def _ctx_with(tmp_path, handler) -> AppContext:
    s = offline_settings(
        tmp_path, offline_mode=False, sec_user_agent="test agent test@example.com", http_max_attempts=2
    )
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db, transport=httpx.MockTransport(handler))
    ctx.http._sleep = lambda _: None
    return ctx


def _fixture_or(fail_host: str, response):
    fx = FixtureTransport(FIXTURES)

    def handler(req: httpx.Request):
        if req.url.host == fail_host:
            return response(req)
        return fx.handle_request(req)

    return handler


@pytest.mark.parametrize(
    "name,response",
    [
        ("timeout", lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("timeout", request=req))),
        ("rate_limited", lambda req: httpx.Response(429, headers={"retry-after": "0"})),
        ("malformed", lambda req: httpx.Response(200, content=b"<html>not a calendar</html>")),
    ],
)
def test_one_failing_source_does_not_abort_others(tmp_path, name, response):
    ctx = _ctx_with(tmp_path, _fixture_or("www.federalreserve.gov", response))
    start, as_of = fixture_window()
    rep = EventIngestor(ctx).run(["fomc", "sec"], as_of=as_of, start=start)
    assert not rep.ok and rep.errors[0].startswith("fomc:")
    assert rep.per_source["sec"]["inserted"] > 0  # SEC still ingested


def test_missing_sec_identity_is_reported(tmp_path):
    s = offline_settings(tmp_path, offline_mode=False, sec_user_agent=None)
    db = Database(s.database_url)
    db.create_all()
    rep = EventIngestor(AppContext(s, db=db)).run(["sec"], start=date(2026, 10, 1))
    assert not rep.ok and "SEC_USER_AGENT" in rep.errors[0]


def test_concurrent_upserts_of_same_event_create_one_row(tmp_path):
    from tests.unit.test_events_core import macro_event

    s = offline_settings(tmp_path)
    db = Database(s.database_url)
    db.create_all()
    errors, barrier = [], threading.Barrier(4)

    def work():
        barrier.wait()
        try:
            EventStore(db).upsert(macro_event())
        except Exception as exc:  # pragma: no cover
            errors.append(repr(exc))

    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    with db.session() as sess:
        assert sess.scalar(select(func.count()).select_from(FinancialEventRecord)) == 1
    assert errors == []


def test_conflicting_reingestion_keeps_original_observed_value(tmp_path):
    from tests.unit.test_events_core import macro_event

    s = offline_settings(tmp_path)
    db = Database(s.database_url)
    db.create_all()
    store = EventStore(db)
    assert store.upsert(macro_event(value=0.4)).inserted == 1
    stats = store.upsert(macro_event(value=0.9))  # SYNTHETIC conflicting value
    assert stats.conflicts and "immutable" in stats.conflicts[0]
    assert store.get(macro_event().event_id).value == pytest.approx(0.4)


# ---------------------------------------------------------------- ranking expectations


def test_curated_ranking_expectations_hold(events_ctx):
    import json

    spec = json.loads((FIXTURES / "eval" / "ranking_expectations.json").read_text(encoding="utf-8"))
    assert spec["independent_labels"] is False  # => regression expectations only; no metric reported
    store = EventStore(events_ctx.db)
    failures = []
    for p in spec["pairs"]:
        hi, lo = store.by_key(p["higher"]), store.by_key(p["lower"])
        assert hi is not None and lo is not None, p
        if not (hi.importance_score or -1) > (lo.importance_score or -1):
            failures.append(
                f"{p['higher']} ({hi.importance_score}) <= {p['lower']} ({lo.importance_score}): {p['rationale']}"
            )
    assert failures == []


def test_scores_are_stable_across_reingestion(events_ctx):
    store = EventStore(events_ctx.db)
    before = {e.event_id: e.importance_score for e in store.query(limit=10000)}
    start, as_of = fixture_window()
    EventIngestor(events_ctx).run(as_of=as_of, start=start)
    after = {e.event_id: e.importance_score for e in store.query(limit=10000)}
    assert before == after
