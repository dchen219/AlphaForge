"""Replay-audit and testing-console read-only endpoints: cutoff integrity over REAL recorded events,
plus SYNTHETIC revision/link cases constructed inline (clearly labelled, far from the real window)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from finance_agent.events.briefing import BriefingRequest
from finance_agent.events.schemas import (
    AvailabilityPrecision,
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    RevisionStatus,
    Unit,
    VerificationStatus,
)
from finance_agent.events.store import EventStore
from finance_agent.interfaces.api import create_app
from finance_agent.schemas.research import ResearchRequest
from finance_agent.workflows.briefing import BRIEFING_NODES, BriefingWorkflow
from finance_agent.workflows.company_research import RESEARCH_NODES, CompanyResearchWorkflow

CUTOFF = "2026-10-28"
T1 = datetime(2019, 3, 12, 12, 30, tzinfo=UTC)  # SYNTHETIC events live in 2019, outside the fixture window


@pytest.fixture(scope="module")
def client(events_ctx):
    with TestClient(create_app(events_ctx.settings, events_ctx)) as c:
        yield c


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def synthetic(key: str, **kw) -> FinancialEvent:
    base = dict(
        canonical_event_key=f"SYNTHETIC|{key}",
        event_type=EventCategory.MACRO_RELEASE,
        event_subtype=EventSubtype.CPI,
        event_status=EventStatus.PUBLISHED,
        title=f"SYNTHETIC {key}",
        source_name="SYNTHETIC test",
        available_at=T1,
        published_at=T1,
        availability_precision=AvailabilityPrecision.EXACT_TIMESTAMP,
        value=0.3,
        unit=Unit.PERCENT_CHANGE,
        revision_status=RevisionStatus.FIRST_RELEASE,
        verification_status=VerificationStatus.SOURCE_VERIFIED,
        data_origin="fixture:synthetic",
        affected_entities=["SYNTHETIC"],
    )
    base.update(kw)
    return FinancialEvent(**base)


def test_visible_set_equals_point_in_time_event_query(client):
    r = client.get("/events/replay", params={"as_of": CUTOFF, "from": "2026-10-01"})
    assert r.status_code == 200
    body = r.json()
    cut = _ts(body["as_of"])
    visible = {d["event"]["event_id"] for d in body["visible"]}
    listed = client.get(
        "/events", params={"as_of": CUTOFF, "from": "2026-10-01", "to": CUTOFF, "limit": 1000}
    )
    assert visible == {e["event_id"] for e in listed.json()["events"]} and visible
    assert all(_ts(d["event"]["available_at"]) <= cut for d in body["visible"])
    assert all(d["visible"] and "<= cutoff" in d["reason"] for d in body["visible"])


def test_excluded_events_are_strictly_after_cutoff_and_hidden(client):
    body = client.get("/events/replay", params={"as_of": CUTOFF, "horizon_days": 30}).json()
    cut, horizon = _ts(body["as_of"]), _ts(body["horizon_end"])
    assert body["excluded_future"], "the recorded window extends past the cutoff"
    visible = {d["event"]["event_id"] for d in body["visible"]}
    for d in body["excluded_future"]:
        at = _ts(d["event"]["available_at"])
        assert cut < at <= horizon and not d["visible"] and d["event"]["event_id"] not in visible
    eid = body["excluded_future"][0]["event"]["event_id"]
    assert client.get(f"/events/{eid}", params={"as_of": CUTOFF}).status_code == 404
    assert client.get(f"/events/{eid}").status_code == 200


def test_calendar_knowledge_respects_cutoff(client):
    body = client.get("/events/replay", params={"as_of": CUTOFF}).json()
    cut = _ts(body["as_of"])
    for c in body["calendar"]:
        seen = _ts(c["first_observed_at"]) if c["first_observed_at"] else None
        if c["status"] == "withheld_observed_later":
            assert seen is None or seen > cut
        else:
            assert seen is not None and seen <= cut
        assert c["event"]["event_status"] == "SCHEDULED"


def test_replay_withheld_count_matches_briefing_workflow(client, events_ctx):
    """The UI audit and the briefing share one calendar-visibility implementation."""
    rep = BriefingWorkflow(events_ctx).run(BriefingRequest(date=CUTOFF, use_llm=False))["report"]
    body = client.get("/events/replay", params={"as_of": CUTOFF, "from": CUTOFF}).json()
    withheld = sum(c["status"] == "withheld_observed_later" for c in body["calendar"])
    msg = next((u for u in rep.uncertainty if u.startswith("Release-calendar entries withheld")), None)
    assert (int(re.search(r"withheld: (\d+)", msg).group(1)) if msg else 0) == withheld
    unconfirmed = {c["event"]["title"] for c in body["calendar"] if c["status"] == "known_due_unconfirmed"}
    assert all(any(t in u for u in rep.uncertainty) for t in unconfirmed)


def test_replay_input_validation(client):
    assert client.get("/events/replay").status_code == 422  # cutoff required
    assert client.get("/events/replay", params={"as_of": "nope"}).status_code == 422
    assert client.get("/events/replay", params={"as_of": CUTOFF, "from": "2026-11-30"}).status_code == 422
    assert client.get("/events/replay", params={"as_of": CUTOFF, "horizon_days": 0}).status_code == 422
    assert client.get("/events/EV-0123456789abcdef", params={"as_of": "bad"}).status_code in (404, 422)


def test_synthetic_revision_history_is_point_in_time(client, events_ctx):
    store = EventStore(events_ctx.db)
    v1 = synthetic("CPI|2019-02|FIRST")
    v2 = synthetic(
        "CPI|2019-02|REV1",
        event_status=EventStatus.REVISED,
        revision_status=RevisionStatus.REVISION,
        available_at=T1 + timedelta(days=30),
        published_at=T1 + timedelta(days=30),
        value=0.4,
        supersedes_event_id=v1.event_id,
    )
    store.upsert(v1)
    store.upsert(v2)
    full = client.get(f"/events/{v1.event_id}").json()
    assert [h["event_id"] for h in full["revision_history"]] == [v1.event_id, v2.event_id]
    between = (T1 + timedelta(days=1)).isoformat()
    early = client.get(f"/events/{v1.event_id}", params={"as_of": between}).json()
    assert early["revision_history"] == [] and early["event"]["value"] == 0.3  # first release preserved
    assert client.get(f"/events/{v2.event_id}", params={"as_of": between}).status_code == 404
    late = client.get(f"/events/{v2.event_id}", params={"as_of": (T1 + timedelta(days=31)).isoformat()})
    assert late.json()["supersedes"]["event_id"] == v1.event_id


def test_synthetic_link_to_later_event_is_masked_and_explained(client, events_ctx):
    store = EventStore(events_ctx.db)
    later = synthetic(
        "LINK|LATER", available_at=T1 + timedelta(days=60), published_at=T1 + timedelta(days=60)
    )
    early = synthetic(
        "LINK|EARLY",
        available_at=T1 + timedelta(days=50),
        published_at=T1 + timedelta(days=50),
        related_event_id=later.event_id,
    )
    store.upsert(later)
    store.upsert(early)
    cut = (T1 + timedelta(days=55)).isoformat()
    body = client.get(
        "/events/replay", params={"as_of": cut, "from": (T1 + timedelta(days=45)).isoformat()}
    ).json()
    d = next(x for x in body["visible"] if x["event"]["event_id"] == early.event_id)
    assert d["event"]["related_event_id"] is None
    assert d["masked_links"] == {"related_event_id": later.event_id} and "masked" in d["reason"]
    assert later.event_id in {x["event"]["event_id"] for x in body["excluded_future"]}
    assert all(x["event"]["data_origin"] == "fixture:synthetic" for x in [d])
    detail = client.get(f"/events/{early.event_id}", params={"as_of": cut}).json()
    assert detail["event"]["related_event_id"] is None


def test_event_subtype_filter(client):
    r = client.get("/events", params={"subtype": "FORM_8K", "from": "2025-07-01", "limit": 1000})
    assert r.status_code == 200 and all(e["event_subtype"] == "FORM_8K" for e in r.json()["events"])
    assert client.get("/events", params={"subtype": "NOT_A_SUBTYPE"}).status_code == 422


def test_run_records_listing_and_node_timestamps(client, events_ctx):
    CompanyResearchWorkflow(events_ctx).run(ResearchRequest(ticker="MSFT", quarters=4, use_llm=False))
    BriefingWorkflow(events_ctx).run(BriefingRequest(date=CUTOFF, use_llm=False))
    assert client.get("/workflows").json() == {
        "company_research": list(RESEARCH_NODES),
        "daily_briefing": list(BRIEFING_NODES),
    }
    runs = client.get("/research", params={"limit": 5}).json()
    assert runs["count"] >= 1
    top = runs["runs"][0]
    assert (
        top["ticker"] == "MSFT" and top["use_llm"] is False and top["steps_executed"] == len(RESEARCH_NODES)
    )
    detail = client.get(f"/research/{top['run_id']}").json()
    assert detail["params"]["quarters"] == 4
    for s in detail["steps"]:
        assert _ts(s["started_at"]) <= _ts(s["finished_at"]) and s["ms"] >= 0
    assert client.get("/research", params={"status": "bogus"}).status_code == 422

    bl = client.get("/intelligence/briefings", params={"limit": 3}).json()
    assert bl["count"] >= 1 and bl["briefings"][0]["llm_status"] == "unavailable"
    b = client.get(f"/intelligence/briefings/{bl['briefings'][0]['briefing_id']}").json()
    assert [s["step"] for s in b["steps"]] == list(BRIEFING_NODES)[: len(b["steps"])]
    assert all("started_at" in s for s in b["steps"]) and b["params"]["use_llm"] is False


def test_health_exposes_fixture_window_offline_without_secrets(client):
    h = client.get("/health").json()
    assert h["offline_mode"] is True and h["fixture_event_window"]["start"] == "2025-07-01"
    assert set(h["credentials_configured"]) == {"fred_api_key", "sec_user_agent", "anthropic_api_key"}
