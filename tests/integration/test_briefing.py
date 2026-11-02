"""Daily briefing workflow, API and CLI on REAL recorded snapshots (offline, no LLM credits)."""

from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from finance_agent.agents.llm import NullLLM
from finance_agent.events.briefing import BriefingRequest
from finance_agent.interfaces.api import create_app
from finance_agent.timeutil import ET
from finance_agent.workflows.briefing import BriefingWorkflow, resolve_window

DAY = "2026-10-28"  # FOMC decision + MSFT/other earnings in the real fixtures


@pytest.fixture(scope="module")
def briefing(events_ctx):
    events_ctx.llm = NullLLM("disabled in tests")
    state = BriefingWorkflow(events_ctx).run(BriefingRequest(date=DAY, use_llm=False))
    assert not state.get("error"), state.get("error")
    return state["report"]


def test_window_uses_new_york_day_and_end_of_day_cutoff():
    as_of, start = resolve_window(BriefingRequest(date=DAY))
    assert as_of == datetime(2026, 10, 28, 23, 59, 59, tzinfo=ET)
    assert start == datetime(2026, 10, 28, 0, 0, tzinfo=ET)


def test_briefing_sections_and_verification(briefing):
    assert briefing.verification.status in {"passed", "passed_with_warnings"}
    names = {c.name: c.status for c in briefing.verification.checks}
    for check in ("numeric_claims", "evidence_references", "as_of_compliance", "occurrence_status"):
        assert names[check] == "passed", (check, briefing.verification.checks)
    for sec in [
        "## A. Date and coverage",
        "## B. Executive summary",
        "## C. Top consequential macroeconomic events",
        "## D. Important corporate disclosures",
        "## E. Economic implications",
        "## F. Possibly affected markets",
        "## G. Uncertainty and missing information",
        "## H. Sources",
    ]:
        assert sec in briefing.markdown
    assert "OFFLINE REPLAY of saved REAL official source snapshots" in briefing.markdown
    assert briefing.llm["status"] == "unavailable"


def test_fomc_decision_is_top_macro_event(briefing):
    top = briefing.macro_entries[0]
    assert top.category == "CENTRAL_BANK" and top.subtype.startswith("RATE_")
    assert "Target range for the federal funds rate" in top.what_was_reported
    assert top.why_it_matters and all(w.startswith("Hypothesis") for w in top.why_it_matters)


def test_earnings_8k_and_10q_counted_once(briefing):
    msft = [e for e in briefing.corporate_entries if "MSFT" in e.affected]
    clusters = {e.cluster_id for e in msft}
    assert len(clusters) == len(msft)  # each economic cluster appears once
    earn = next(e for e in msft if e.cluster_id.startswith("CL-EARN-"))
    assert len(earn.event_ids) >= 2 and any("FORM_10Q" in r for r in earn.related)


def test_every_entry_answers_required_questions(briefing):
    for e in briefing.macro_entries + briefing.corporate_entries:
        assert e.what_happened and e.when and e.what_was_reported and e.evidence_ids and e.source_urls
        assert e.importance_explanation


def test_no_consensus_surprise_or_market_reaction_claims(briefing):
    md = briefing.markdown.lower()
    assert "consensus forecasts are unavailable" in md
    assert "no realized market reaction is claimed" in md
    assert "beat" not in md and "missed expectations" not in md


def test_upcoming_section_is_schedule_only(events_ctx):
    state = BriefingWorkflow(events_ctx).run(BriefingRequest(date="2026-11-01", use_llm=False))
    rep = state["report"]
    assert rep.upcoming and all(u["status"] == "SCHEDULED (not occurred)" for u in rep.upcoming)
    assert "NOT events that occurred" in rep.markdown


def test_llm_narrative_is_screened(events_ctx):
    class FakeLLM:
        name, available = "fake-llm", True

        def complete_json(self, **kw):
            first_id = kw["user"].split(" | ")[0]
            return {
                "executive_summary": "Policy tightening dominated the day.",
                "interpretations": [
                    {
                        "text": "The decision may weigh on long-duration assets (hypothesis).",
                        "event_ids": [first_id],
                    },
                    {"text": "Yields jumped 37 bp after the decision.", "event_ids": [first_id]},
                    {"text": "Strong quarter.", "event_ids": ["EV-0000000000000000"]},
                ],
            }

    events_ctx.llm = FakeLLM()
    rep = BriefingWorkflow(events_ctx).run(BriefingRequest(date=DAY, use_llm=True))["report"]
    events_ctx.llm = NullLLM("disabled in tests")
    assert rep.llm["status"] == "generated" and len(rep.model_interpretations) == 1
    assert "37 bp" not in rep.markdown  # fabricated market move dropped
    screening = next(c for c in rep.verification.checks if c.name == "llm_output_screening")
    assert screening.status == "warning" and len(screening.details) == 2


# ---------------------------------------------------------------- API


@pytest.fixture(scope="module")
def client(events_ctx):
    s = events_ctx.settings.model_copy(update={"ingest_admin_token": None})
    events_ctx.llm = NullLLM("disabled in tests")
    with TestClient(create_app(s, events_ctx)) as c:
        yield c


def test_api_events_filters(client):
    r = client.get("/events", params={"from": "2026-10-27", "to": "2026-10-28", "category": "CENTRAL_BANK"})
    assert r.status_code == 200 and r.json()["count"] >= 1
    assert all(e["event_type"] == "CENTRAL_BANK" for e in r.json()["events"])
    r = client.get(
        "/events", params={"ticker": "MSFT", "as_of": "2026-10-28T20:05:00Z", "from": "2026-10-28"}
    )
    ids = {e["source_document_id"] for e in r.json()["events"]}
    assert "0001193125-26-436122" in ids and "0001193125-26-436133" not in ids  # 10-Q accepted 20:07:39Z


def test_api_event_detail_important_and_calendar(client):
    ev = client.get("/events", params={"category": "CENTRAL_BANK", "limit": 1}).json()["events"][0]
    d = client.get(f"/events/{ev['event_id']}").json()
    assert d["event"]["event_id"] == ev["event_id"] and d["sources"]
    imp = client.get(
        "/events/important", params={"as_of": "2026-10-31", "days": 30, "min_level": "MEDIUM"}
    ).json()
    assert imp["count"] >= 1 and all(e["importance_level"] in {"HIGH", "MEDIUM"} for e in imp["events"])
    cal = client.get("/events/calendar", params={"from": "2026-11-01", "to": "2026-12-31"}).json()
    assert cal["count"] >= 1 and all(e["event_status"] == "SCHEDULED" for e in cal["events"])


@pytest.mark.parametrize(
    "path,code",
    [
        ("/events/EV-123", 422),
        ("/events/EV-0000000000000000", 404),
        ("/events?as_of=yesterday", 422),
        ("/events?level=EXTREME", 422),
        ("/events/calendar?from=2026-12-01&to=2026-11-01", 422),
        ("/intelligence/briefings/not-a-uuid", 422),
        ("/intelligence/briefings/00000000-0000-0000-0000-000000000000", 404),
    ],
)
def test_api_validation(client, path, code):
    assert client.get(path).status_code == code


def test_api_briefing_roundtrip(client):
    r = client.post("/intelligence/briefing", json={"date": DAY, "use_llm": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "completed" and body["report"]["macro_entries"]
    again = client.get(f"/intelligence/briefings/{body['briefing_id']}").json()
    assert again["report"]["briefing_id"] == body["briefing_id"]
    assert client.post("/intelligence/briefing", json={"date": DAY, "max_macro": 0}).status_code == 422


def test_api_ingest_is_protected(events_ctx):
    s = events_ctx.settings.model_copy(update={"ingest_admin_token": None})
    with TestClient(create_app(s, events_ctx)) as c:
        assert c.post("/intelligence/ingest", json={"sources": ["fomc"]}).status_code == 403
    from pydantic import SecretStr

    events_ctx.settings = events_ctx.settings.model_copy(update={"ingest_admin_token": SecretStr("s3cret")})
    try:
        with TestClient(create_app(events_ctx.settings, events_ctx)) as c:
            assert c.post("/intelligence/ingest", json={"sources": ["fomc"]}).status_code == 401
            assert (
                c.post(
                    "/intelligence/ingest", json={"sources": ["fomc"]}, headers={"X-Admin-Token": "wrong"}
                ).status_code
                == 401
            )
            assert (
                c.post(
                    "/intelligence/ingest", json={"sources": ["evil"]}, headers={"X-Admin-Token": "s3cret"}
                ).status_code
                == 422
            )
            ok = c.post(
                "/intelligence/ingest",
                json={"sources": ["fomc"], "start": "2026-09-01"},
                headers={"X-Admin-Token": "s3cret"},
            )
            assert ok.status_code == 200 and ok.json()["ok"]
    finally:
        events_ctx.settings = events_ctx.settings.model_copy(update={"ingest_admin_token": None})


# ---------------------------------------------------------------- CLI


def test_cli_events_and_briefing(tmp_path, monkeypatch):
    from finance_agent.config.settings import get_settings
    from finance_agent.interfaces.cli import app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'cli.db').as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        r = runner.invoke(app, ["events", "ingest", "--source", "fomc", "--offline", "--start", "2026-09-01"])
        assert r.exit_code == 0, r.output
        assert "fomc: inserted=" in r.output
        r = runner.invoke(app, ["events", "list", "--from", "2026-09-01", "--to", "2026-11-01", "--offline"])
        assert r.exit_code == 0 and "CENTRAL_BANK" in r.output
        r = runner.invoke(
            app, ["events", "calendar", "--from", "2026-11-01", "--to", "2026-12-31", "--offline"]
        )
        assert r.exit_code == 0 and "MEETING_SCHEDULED" in r.output
        r = runner.invoke(
            app, ["briefing", "daily", "--date", "2026-10-28", "--no-llm", "--offline", "--no-ingest"]
        )
        assert r.exit_code == 0, r.output
        assert "AlphaForge Daily Intelligence Briefing" in r.output
        assert runner.invoke(app, ["events", "ingest", "--source", "evil", "--offline"]).exit_code == 1
        assert runner.invoke(app, ["briefing", "daily", "--date", "28/10/2026", "--offline"]).exit_code == 1
        assert (
            runner.invoke(app, ["financials", "MSFT", "--quarters", "2", "--offline"]).exit_code == 0
        )  # v0.1 intact
    finally:
        get_settings.cache_clear()
