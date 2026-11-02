"""Streamlit smoke + interaction tests: every page renders and its controls hit the real API.

The API is the real FastAPI app on a loopback port over REAL recorded fixtures (see conftest).
No Anthropic credentials exist in tests, so LLM requests exercise the deterministic fallback.
"""

from __future__ import annotations

from datetime import date

import pytest

from finance_agent.events.schemas import (
    AvailabilityPrecision,
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    VerificationStatus,
)
from finance_agent.events.store import EventStore
from finance_agent.timeutil import parse_as_of, parse_window_start

pytest.importorskip("streamlit")
from streamlit.testing.v1 import AppTest

PAGES = ["company_research", "events_explorer", "daily_briefing", "historical_replay", "execution_inspector"]
TIMEOUT = 180


def page(name: str) -> AppTest:
    at = AppTest.from_string(
        f"from finance_agent.ui.pages import {name}\n{name}.render()\n", default_timeout=TIMEOUT
    )
    return at.run()


def click(at: AppTest, label: str) -> AppTest:
    next(b for b in at.button if b.label == label).click()
    return at.run()


def texts(elements) -> str:
    return "\n".join(str(e.value) for e in elements)


def assert_clean(at: AppTest) -> None:
    assert not at.exception, [e.value for e in at.exception]
    assert not at.error, texts(at.error)


@pytest.mark.parametrize("name", PAGES)
def test_every_page_renders(ui_env, name):
    at = page(name)
    assert_clean(at)
    assert at.title and "API http://127.0.0.1" in texts(at.caption)


def test_app_entrypoint_renders_with_navigation(ui_env):
    at = AppTest.from_file("../../apps/ui/app.py", default_timeout=TIMEOUT).run()
    assert not at.exception
    assert at.title[0].value == "Company Research"
    assert "offline" in texts(at.sidebar.caption)


def test_live_mode_unreachable_backend_is_reported_not_crashed(ui_env):
    at = AppTest.from_string(
        "import streamlit as st\nst.session_state['_data_mode'] = st.session_state.get('_data_mode', 'live')\n"
        "from finance_agent.ui.pages import company_research\ncompany_research.render()\n",
        default_timeout=TIMEOUT,
    ).run()
    assert not at.exception
    assert "Live API unavailable" in texts(at.error)
    assert any("serve --port 8001" in c.value for c in at.code)
    assert not [b for b in at.button if b.label == "Run company research"]  # no actions offered


def test_mode_mismatch_refuses_to_send_requests(ui_env, monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_LIVE_API_URL", ui_env["url"])  # an OFFLINE server posing as live
    at = AppTest.from_string(
        "import streamlit as st\nst.session_state['_data_mode'] = 'live'\n"
        "from finance_agent.ui.pages import events_explorer\nevents_explorer.render()\n",
        default_timeout=TIMEOUT,
    ).run()
    assert "does not match" in texts(at.error)


def test_remote_backend_url_is_rejected(ui_env, monkeypatch):
    monkeypatch.setenv("ALPHAFORGE_API_URL", "http://example.com:8000")
    at = page("company_research")
    assert "not a loopback address" in texts(at.error)


# ---------------------------------------------------------------- Page A


def test_company_fundamentals_offline(ui_env):
    at = page("company_research")
    at.text_input(key="A_ticker").set_value("msft")
    at = click(at, "Retrieve fundamentals")
    assert_clean(at)
    fin = at.session_state["A_fin"].financials
    assert fin.ticker == "MSFT" and fin.data_origin == "fixture:real_snapshot"
    assert "HISTORICAL REAL SNAPSHOT" in texts(at.info)
    assert len(at.dataframe) >= 3


def test_company_research_without_anthropic_credit_uses_fallback(ui_env):
    at = page("company_research")
    at.text_input(key="A_ticker").set_value("AAPL")
    at.number_input(key="A_q").set_value(4)
    at.toggle(key="A_llm").set_value(True)  # requested, but no credentials exist
    at = at.run()
    assert "Anthropic key configured: False" in texts(at.info)
    at = click(at, "Run company research")
    assert not at.exception
    run = at.session_state["A_run"]
    assert run.status == "completed" and run.report.llm.status in {"unavailable", "failed"}
    assert run.report.llm.provider == "none" or run.report.model_interpretations == []
    assert "deterministic template summary used" in texts(at.info)
    assert "Verification:" in texts(at.success) + texts(at.warning) + texts(at.error)


def test_company_research_with_cutoff_respects_as_of(ui_env):
    at = page("company_research")
    at.checkbox(key="A_asof_on").check()
    at = at.run()
    at.date_input(key="A_asof_date").set_value(date(2025, 6, 30))
    at = click(at, "Retrieve fundamentals")
    assert_clean(at)
    fin = at.session_state["A_fin"].financials
    cut = parse_as_of("2025-06-30")
    assert fin.as_of == cut
    for row in fin.series.values():
        for v in row:
            if v:
                assert all(s.available_at <= cut for s in v.sources)


def test_invalid_ticker_is_rejected_before_any_request(ui_env):
    at = page("company_research")
    at.text_input(key="A_ticker").set_value("1BAD")
    at = click(at, "Run company research")
    assert "Invalid input" in texts(at.warning) and "A_run" not in at.session_state


def test_unknown_ticker_api_error_is_informative(ui_env):
    at = page("company_research")
    at.text_input(key="A_ticker").set_value("ZZZZZ")
    at = click(at, "Retrieve fundamentals")
    assert not at.exception
    assert "HTTP 404" in texts(at.error)


# ---------------------------------------------------------------- Page B


def test_events_explorer_search_and_detail(ui_env):
    at = page("events_explorer")
    at = click(at, "Search events")
    assert_clean(at)
    res = at.session_state["B_events"]
    assert res.count > 0
    assert "Importance breakdown" in texts(at.markdown)
    assert any(r.source_url for r in res.events)


def test_events_explorer_cutoff_hides_later_events(ui_env):
    at = page("events_explorer")
    at.checkbox(key="B_asof_on").check()
    at = at.run()
    at.date_input(key="B_asof_date").set_value(date(2026, 10, 15))
    at = click(at, "Search events")
    assert_clean(at)
    cut = parse_as_of("2026-10-15")
    evs = at.session_state["B_events"].events
    assert evs and all(e.available_at <= cut for e in evs)


def test_events_explorer_filters(ui_env):
    at = page("events_explorer")
    at.multiselect(key="B_cat").set_value(["CENTRAL_BANK"])
    at = click(at, "Search events")
    assert_clean(at)
    assert all(str(e.event_type) == "CENTRAL_BANK" for e in at.session_state["B_events"].events)
    at.multiselect(key="B_cat").set_value([])
    at.text_input(key="B_ticker").set_value("not a ticker!")
    at = click(at, "Search events")
    assert "Invalid input" in texts(at.warning)


def test_synthetic_events_are_labelled_synthetic(ui_env):
    """SYNTHETIC event (2019, outside the real fixture window) must render with the synthetic banner."""
    from datetime import UTC, datetime

    ev = FinancialEvent(
        canonical_event_key="SYNTHETIC|UI|2019-01-10",
        event_type=EventCategory.CENTRAL_BANK,
        event_subtype=EventSubtype.RATE_HOLD,
        event_status=EventStatus.PUBLISHED,
        title="SYNTHETIC UI test event",
        source_name="SYNTHETIC test",
        available_at=datetime(2019, 1, 10, 19, 0, tzinfo=UTC),
        availability_precision=AvailabilityPrecision.EXACT_TIMESTAMP,
        verification_status=VerificationStatus.UNVERIFIED,
        data_origin="fixture:synthetic",
    )
    EventStore(ui_env["ctx"].db).upsert(ev)
    at = page("events_explorer")
    at.date_input(key="B_range").set_value((date(2019, 1, 1), date(2019, 1, 31)))
    at = click(at, "Search events")
    assert not at.exception
    assert [e.event_id for e in at.session_state["B_events"].events] == [ev.event_id]
    banner = texts(at.error)
    assert "SYNTHETIC FIXTURE" in banner and "NOT real" in banner
    assert "LIVE" not in texts(at.success)


# ---------------------------------------------------------------- Page C


def test_daily_briefing_offline_without_llm_credentials(ui_env):
    at = page("daily_briefing")
    at.date_input(key="C_date").set_value(date(2026, 10, 28))
    at.toggle(key="C_llm").set_value(True)
    at = click(at, "Generate briefing")
    assert not at.exception
    run = at.session_state["C_run"]
    rep = run.report
    assert run.status == "completed" and rep.llm["status"] in {"unavailable", "failed"}
    assert rep.window_end == parse_as_of("2026-10-28")
    assert "Executive summary" in [s.value for s in at.subheader]
    assert "HISTORICAL REAL SNAPSHOT" in texts(at.info)
    assert any("consensus" in w.value for w in at.warning)


def test_daily_briefing_retrieve_stored(ui_env):
    at = page("daily_briefing")
    at.date_input(key="C_date").set_value(date(2026, 10, 27))
    at = click(at, "Generate briefing")
    bid = at.session_state["C_run"].briefing_id
    at2 = page("daily_briefing")
    at2.selectbox(key="C_pick").set_value(bid)
    at2 = click(at2, "Load briefing")
    assert not at2.exception and at2.session_state["C_run"].briefing_id == bid


# ---------------------------------------------------------------- Page D


def test_historical_replay_respects_cutoff(ui_env):
    at = page("historical_replay")
    at.date_input(key="D_day").set_value(date(2026, 10, 28))
    at = click(at, "Run replay audit")
    assert_clean(at)
    res = at.session_state["D_result"]
    cut = parse_as_of("2026-10-28")
    assert res.as_of == cut and res.visible and res.excluded_future
    assert all(d.event.available_at <= cut for d in res.visible)
    assert all(d.event.available_at > cut for d in res.excluded_future)
    assert "Cutoff integrity" in texts(at.success)
    assert res.window_start == parse_window_start("2026-10-21")  # 7 days back, 00:00 New York


# ---------------------------------------------------------------- Page E


def test_execution_inspector_shows_real_records(ui_env):
    at = page("company_research")
    at.text_input(key="A_ticker").set_value("MSFT")
    at = click(at, "Run company research")
    run_id = at.session_state["A_run"].run_id

    at = page("execution_inspector")
    assert_clean(at)
    at.selectbox(key="E_pick").set_value(run_id)
    at = at.run()
    assert_clean(at)
    nodes = at.dataframe[1].value  # [0] = run list, [1] = node table
    assert list(nodes["node"])[:3] == ["parse_request", "resolve_company", "plan_metrics"]
    assert set(nodes["executed"]) == {"yes"} and "not recorded" not in set(nodes["started_at"])

    at.radio(key="E_kind").set_value("daily_briefing")
    at = at.run()
    assert not at.exception
