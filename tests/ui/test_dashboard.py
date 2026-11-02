"""Dashboard, Market Data, Agent Testing Lab and two-cutoff replay — driven against the real API.

Prices are SYNTHETIC fixtures (labelled); yields and events are REAL recorded snapshots.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from finance_agent.timeutil import ET, parse_as_of

pytest.importorskip("streamlit")
from tests.ui.test_pages import assert_clean, click, page, texts

# ---------------------------------------------------------------- Dashboard


def test_dashboard_shows_health_sources_and_missing_credentials(ui_env):
    at = page("dashboard")
    assert_clean(at)
    health = at.dataframe[0].value
    assert list(health["backend"]) == ["offline", "live"]
    assert list(health["reachable"]) == ["yes", "no"]  # live points at a discard port: reported
    sources = at.dataframe[1].value
    creds = " ".join(sources["credentials"])
    assert "ANTHROPIC_API_KEY: MISSING" in creds and "ALPACA_API_KEY_ID: MISSING" in creds
    assert "SEC_USER_AGENT: MISSING" in creds
    labels = dict(zip(sources["source"].str.split(" ").str[0], sources["offline label"], strict=False))
    assert labels["Alpaca"].startswith("SYNTHETIC")
    assert labels["SEC"].startswith("HISTORICAL REAL SNAPSHOT")
    assert any("Not configured" in w.value for w in at.warning)
    # Ingestion state: event store per source and the synthetic market run, labelled.
    events = at.dataframe[2].value
    assert events["events"].sum() > 0
    runs = at.dataframe[3].value
    assert (runs["origin"] == "fixture:synthetic").any()
    assert any("SYNTHETIC" in e.value for e in at.error)


# ---------------------------------------------------------------- Market Data


def _market() -> object:
    at = page("market_data")
    assert_clean(at)
    assert any("Offline prices are **SYNTHETIC**" in e.value for e in at.error)
    return at


def test_market_prices_label_synthetic_and_show_explicit_basis(ui_env):
    at = _market()
    at.selectbox(key="MP_t").set_value("NVDA")
    at.radio(key="MP_basis").set_value("split_adjusted")
    at = click(at, "Load prices")
    assert_clean(at)
    res = at.session_state["MP_res"]
    assert res.contract.basis == "split_adjusted" and res.contract.query_mode == "point_in_time"
    assert set(res.data_origins) == {"fixture:synthetic"}
    assert any("SYNTHETIC PRICE DATA" in e.value for e in at.error)
    assert "**Price basis:** `split_adjusted`" in texts(at.markdown)


def test_market_point_in_time_cutoff_propagates(ui_env):
    at = _market()
    at.checkbox(key="MP_asof_on").check()
    at = at.run()
    at.date_input(key="MP_asof_date").set_value(date(2026, 6, 30))
    at = click(at, "Load prices")
    assert_clean(at)
    res = at.session_state["MP_res"]
    cut = parse_as_of("2026-06-30")
    assert res.contract.as_of == cut
    bars = [r for r in res.rows if r.get("close") is not None]
    assert bars and all(datetime.fromisoformat(r["available_at"]) <= cut for r in bars)
    assert max(r["session_date"] for r in res.rows) <= "2026-06-30"


def test_market_research_mode_takes_no_cutoff_and_says_retrospective(ui_env):
    at = _market()
    at.radio(key="MP_mode").set_value("research")
    at = at.run()
    assert not [c for c in at.checkbox if c.key == "MP_asof_on"]
    at.radio(key="MP_basis").set_value("total_return")
    at = click(at, "Load prices")
    assert_clean(at)
    res = at.session_state["MP_res"]
    assert res.contract.retrospectively_adjusted and res.contract.as_of is None
    assert any("RETROSPECTIVE" in w.value for w in at.warning)


def test_market_returns_computed_server_side(ui_env):
    at = _market()
    at.selectbox(key="MR_t").set_value("SPY")
    at.radio(key="MR_basis").set_value("total_return")
    at = click(at, "Compute returns (server-side)")
    assert_clean(at)
    res = at.session_state["MR_res"]
    assert res.contract.basis == "total_return" and res.returns
    assert "Cumulative return" in [m.label for m in at.metric]


def test_market_yields_are_real_snapshots_with_vintage_cutoff(ui_env):
    at = _market()
    at.selectbox(key="MY_sid").set_value("DGS10")
    at.checkbox(key="MY_asof_on").check()
    at = at.run()
    at.date_input(key="MY_asof_date").set_value(date(2026, 10, 15))
    at = click(at, "Load yields")
    assert_clean(at)
    res = at.session_state["MY_res"]
    assert res.series_id == "DGS10" and res.as_of == parse_as_of("2026-10-15")
    assert {o["data_origin"] for o in res.observations} == {"fixture:real_snapshot"}
    assert all(datetime.fromisoformat(o["available_at"]) <= res.as_of for o in res.observations)
    assert any("HISTORICAL REAL SNAPSHOT" in i.value for i in at.info)


def test_market_calendar_and_event_alignment(ui_env):
    at = _market()
    at = click(at, "Inspect calendar")
    assert_clean(at)
    res = at.session_state["MC_res"]
    assert res.is_session is False  # 2026-11-26: Thanksgiving
    assert res.alignment and res.alignment["phase"] == "REGULAR"  # 14:00 New York on a session


def test_market_calendar_rejects_bad_event_id(ui_env):
    at = _market()
    at.text_input(key="MC_evid").set_value("not-an-id")
    at = click(at, "Inspect calendar")
    assert "MC_res" not in at.session_state
    assert any("Event ids look like" in w.value for w in at.warning)


def test_market_quality_flags(ui_env):
    at = _market()
    at = click(at, "Load quality flags")
    assert_clean(at)
    assert at.session_state["MQ_res"].runs


def test_market_range_validation_blocks_request(ui_env):
    at = _market()
    at.date_input(key="MP_start").set_value(date(2026, 10, 1))
    at.date_input(key="MP_end").set_value(date(2026, 9, 1))
    at = click(at, "Load prices")
    assert "MP_res" not in at.session_state
    assert any("start date must be on or before" in w.value for w in at.warning)


# ---------------------------------------------------------------- Agent Testing Lab


def test_agent_lab_executes_research_and_reports_timing(ui_env):
    at = page("agent_lab")
    at.text_input(key="EX_ticker").set_value("MSFT")
    at = click(at, "Execute workflow")
    assert_clean(at)
    kind, run, elapsed, exc = at.session_state["EX_result"]
    assert kind == "company_research" and exc is None and elapsed > 0
    assert run.report is not None and run.report.verification.status
    assert run.report.llm.status == "unavailable"  # no Anthropic credentials: deterministic fallback
    assert "Round-trip time (measured by this dashboard)" in [m.label for m in at.metric]
    assert at.get("download_button")  # report export offered


def test_agent_lab_executes_briefing(ui_env):
    at = page("agent_lab")
    at.radio(key="E_kind").set_value("daily_briefing")
    at = at.run()
    at.date_input(key="EX_day").set_value(date(2026, 10, 28))
    at = click(at, "Execute workflow")
    assert_clean(at)
    kind, run, _, exc = at.session_state["EX_result"]
    assert kind == "daily_briefing" and exc is None and run.status == "completed"


def test_agent_lab_reports_workflow_failure_with_timing(ui_env):
    """An unknown ticker is a recorded *failed* run (HTTP 200), shown with its error and nodes."""
    at = page("agent_lab")
    at.text_input(key="EX_ticker").set_value("ZZZZQ")
    at = click(at, "Execute workflow")
    assert not at.exception
    _, run, elapsed, exc = at.session_state["EX_result"]
    assert exc is None and run.status == "failed" and run.report is None and elapsed > 0
    assert any("ZZZZQ" in e.value for e in at.error)
    nodes = at.dataframe[0].value
    assert "fail" in set(nodes["node"]) or "error" in set(nodes["status"])


def test_agent_lab_blocks_invalid_input_before_any_request(ui_env):
    at = page("agent_lab")
    at.text_input(key="EX_ticker").set_value("$$$")
    at = click(at, "Execute workflow")
    _, run, _, exc = at.session_state["EX_result"]
    assert run is None and exc is not None
    assert any("not a valid ticker" in w.value for w in at.warning)


def test_agent_lab_compares_runs_at_different_cutoffs(ui_env):
    for cut in (None, date(2025, 6, 30)):
        at = page("agent_lab")
        at.text_input(key="EX_ticker").set_value("MSFT")
        if cut:
            at.checkbox(key="EX_asof_on").check()
            at = at.run()
            at.date_input(key="EX_asof_date").set_value(cut)
        at = click(at, "Execute workflow")
        assert at.session_state["EX_result"][3] is None
    at = page("agent_lab")
    at = click(at, "Compare")
    assert_clean(at)
    _, ra, rb = at.session_state["CMP_res"]
    assert ra.run_id != rb.run_id
    summary = {r["field"]: r for r in at.dataframe[-2].value.to_dict("records")}
    assert summary["as_of"]["differs"] == "yes"
    values = at.dataframe[-1].value
    assert {"metric", "period", "run A", "run B", "differs"} <= set(values.columns)
    assert (values["differs"] == "yes").any()  # later cutoff sees more periods


# ---------------------------------------------------------------- Historical Replay: two cutoffs


def _compare(query: str, a: date, b: date, **widgets) -> object:
    at = page("historical_replay")
    at.selectbox(key="DC_query").set_value(query)
    at = at.run()
    at.date_input(key="DA_day").set_value(a)
    at.date_input(key="DB_day").set_value(b)
    for k, v in widgets.items():
        (at.text_input(key=k) if isinstance(v, str) and k.endswith("t") else at.date_input(key=k)).set_value(
            v
        )
    at = click(at, "Run at both cutoffs")
    assert_clean(at)
    return at


def test_replay_compare_events_between_cutoffs(ui_env):
    at = _compare("events", date(2026, 10, 14), date(2026, 10, 28))
    _, ra, rb = at.session_state["DC_res"]
    assert ra.as_of == parse_as_of("2026-10-14") and rb.as_of == parse_as_of("2026-10-28")
    assert sum("every returned record" in s.value for s in at.success) == 2
    changes = set(at.dataframe[-1].value["change"])
    assert "only at B" in changes and "only at A" not in changes


def test_replay_compare_financials_respects_both_cutoffs(ui_env):
    at = _compare("financials", date(2025, 6, 30), date(2026, 10, 30), DC_ft="MSFT")
    _, fa, fb = at.session_state["DC_res"]
    assert fa.financials.as_of == parse_as_of("2025-06-30")
    for data in (fa, fb):
        cut = data.financials.as_of
        assert all(
            s.available_at <= cut
            for vals in data.financials.series.values()
            for v in vals
            if v
            for s in v.sources
        )
    assert "only at B" in set(at.dataframe[-1].value["change"])


def test_replay_compare_yields_by_vintage(ui_env):
    at = _compare("yields", date(2026, 10, 15), date(2026, 10, 30))
    _, ya, yb = at.session_state["DC_res"]
    assert len(yb.observations) > len(ya.observations)
    assert max(o["observation_date"] for o in ya.observations) < "2026-10-15"


def test_replay_compare_prices_labels_synthetic(ui_env):
    at = _compare("prices", date(2026, 10, 1), date(2026, 10, 30))
    _, pa, _ = at.session_state["DC_res"]
    assert pa.contract.as_of.astimezone(ET).date() == date(2026, 10, 1)
    assert any("SYNTHETIC PRICE DATA" in e.value for e in at.error)
    assert "only at B" in set(at.dataframe[-1].value["change"])
