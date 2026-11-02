"""Historical Replay: what a run at a cutoff can see and why, and how answers change between cutoffs."""

from __future__ import annotations

from datetime import date, time, timedelta

import streamlit as st

from finance_agent.events.schemas import EventCategory
from finance_agent.interfaces.events_api import ReplayOut
from finance_agent.ui import compare, validation
from finance_agent.ui import components as ui

QUERIES = {
    "events": "Event visibility (GET /events/replay)",
    "financials": "Company financials (GET /companies/{ticker}/financials?as_of=)",
    "prices": "Market prices, point-in-time (GET /market/prices/{ticker}?as_of=)",
    "yields": "Treasury yields by vintage (GET /market/yields/{series}?as_of=)",
}


def render() -> None:
    st.title("Historical Replay")
    st.caption(
        "Point-in-time answers computed by the backend. Visibility is never decided in the UI; the page "
        "only checks the returned records against the cutoff the server echoed back."
    )
    mode = ui.mode_selector("Snapshot / replay data")
    c, h = ui.backend(mode)
    if c is None:
        return
    ui.backend_caption(h, c)
    fixture_day = ui.fixture_cutoff_day(h)
    audit_tab, compare_tab = st.tabs(["Single-cutoff audit", "Compare two cutoffs"])
    with audit_tab:
        _audit(c, fixture_day)
    with compare_tab:
        _compare(c, fixture_day)


def _audit(c, fixture_day: date | None) -> None:
    default = fixture_day - timedelta(days=5) if fixture_day else date.today() - timedelta(days=1)
    col = st.columns([2, 1, 2])
    day = col[0].date_input("Historical cutoff date (New York)", value=default, key="D_day")
    timed = col[1].checkbox("Set time", key="D_timed", help="Unchecked = 23:59:59 New York.")
    at = col[2].time_input("Cutoff time (New York)", key="D_time") if timed else None
    col = st.columns(4)
    lookback = col[0].number_input("Window: days before cutoff", 1, 120, 7, key="D_lookback")
    horizon = col[1].number_input("Diagnostic horizon after cutoff (days)", 1, 365, 30, key="D_horizon")
    cats = col[2].multiselect("Event category", [x.value for x in EventCategory], key="D_cat")
    raw_ticker = col[3].text_input("Ticker (optional)", key="D_ticker", max_chars=10)

    if st.button("Run replay audit", type="primary"):
        try:
            as_of = validation.cutoff(day, at)
            start = (day - timedelta(days=int(lookback))).isoformat()
            with st.spinner("GET /events/replay …"):
                st.session_state["D_result"] = c.replay(
                    as_of,
                    start=start,
                    horizon_days=int(horizon),
                    categories=cats,
                    ticker=validation.ticker(raw_ticker, required=False),
                )
        except Exception as exc:
            ui.show_error(exc, "Replay audit")
    res = st.session_state.get("D_result")
    if res is None:
        st.info("Choose a cutoff and click **Run replay audit**.")
        return
    show_replay(res)


def integrity(res: ReplayOut) -> list[str]:
    """Client-side assertion of the server's decisions (any line returned is a defect)."""
    return compare.replay_violations(res)


def _cutoff_pair(fixture_day: date | None) -> tuple[str | None, str | None]:
    base = fixture_day or date.today()
    out = []
    for label, key, default in (("A", "DA", base - timedelta(days=30)), ("B", "DB", base)):
        col = st.columns([2, 1, 2])
        d = col[0].date_input(f"Cutoff {label} (New York date)", value=default, key=f"{key}_day")
        timed = col[1].checkbox("Set time", key=f"{key}_timed", help="Unchecked = 23:59:59 New York.")
        t = (
            col[2].time_input(f"Cutoff {label} time (New York)", value=time(16, 0), key=f"{key}_time")
            if timed
            else None
        )
        out.append(validation.cutoff(d, t))
    return out[0], out[1]


def _compare(c, fixture_day: date | None) -> None:
    query = st.selectbox("Historical query", list(QUERIES), format_func=QUERIES.get, key="DC_query")
    a, b = _cutoff_pair(fixture_day)
    params: dict = {}
    if query == "events":
        col = st.columns(2)
        params["lookback"] = col[0].number_input("Window: days before cutoff A", 1, 365, 30, key="DC_lb")
        params["ticker"] = col[1].text_input("Ticker (optional)", key="DC_et", max_chars=10)
    elif query == "financials":
        col = st.columns(2)
        params["ticker"] = col[0].text_input("Ticker", value="MSFT", key="DC_ft", max_chars=10)
        params["quarters"] = col[1].number_input("Quarters", 2, 20, 8, key="DC_fq")
    elif query == "prices":
        col = st.columns(4)
        params["ticker"] = col[0].text_input("Ticker", value="NVDA", key="DC_pt", max_chars=10)
        params["basis"] = col[1].selectbox(
            "Price basis", ["raw", "split_adjusted", "total_return"], key="DC_pb"
        )
        params["start"] = col[2].date_input("Start", value=date(2026, 9, 1), key="DC_ps")
        params["end"] = col[3].date_input("End", value=date(2026, 10, 30), key="DC_pe")
    else:
        col = st.columns(3)
        params["series"] = col[0].selectbox("Series", ["DGS10", "DGS2"], key="DC_ys")
        params["start"] = col[1].date_input("Start", value=date(2026, 9, 1), key="DC_yst")
        params["end"] = col[2].date_input("End", value=date(2026, 10, 30), key="DC_ye")
    if st.button("Run at both cutoffs", type="primary", key="DC_go"):
        try:
            with st.spinner("Querying the API at cutoff A and cutoff B …"):
                st.session_state["DC_res"] = (query, *_run_pair(c, query, a, b, params))
        except Exception as exc:
            ui.show_error(exc, "Historical comparison")
    res = st.session_state.get("DC_res")
    if res is None or res[0] != query:
        st.info("Pick a query and two cutoffs, then click **Run at both cutoffs**.")
        return
    _show_pair(*res)


def _run_pair(c, query: str, a: str, b: str, p: dict) -> tuple:
    if query == "events":
        ticker = validation.ticker(p["ticker"], required=False)
        day_a = date.fromisoformat(a[:10])
        start = (day_a - timedelta(days=int(p["lookback"]))).isoformat()
        return c.replay(a, start=start, ticker=ticker), c.replay(b, start=start, ticker=ticker)
    if query == "financials":
        t, q = validation.ticker(p["ticker"]), validation.quarters(p["quarters"])
        return c.financials(t, q, a), c.financials(t, q, b)
    s, e = validation.market_range(p["start"], p["end"])
    if query == "prices":
        t = validation.ticker(p["ticker"])
        args = (t, s.isoformat(), e.isoformat())
        return (
            c.prices(*args, basis=p["basis"], mode="point_in_time", as_of=a),
            c.prices(*args, basis=p["basis"], mode="point_in_time", as_of=b),
        )
    return (
        c.yields(p["series"], s.isoformat(), e.isoformat(), a),
        c.yields(p["series"], s.isoformat(), e.isoformat(), b),
    )


def _verdict(label: str, problems: list[str]) -> None:
    if problems:
        st.error(f"Cutoff {label}: point-in-time integrity VIOLATED\n" + "\n".join(problems[:20]))
    else:
        st.success(f"Cutoff {label}: every returned record has available_at ≤ the cutoff.")


def _show_pair(query: str, ra, rb) -> None:
    if query == "events":
        cuts = (ra.as_of, rb.as_of)
        checks = (compare.replay_violations(ra), compare.replay_violations(rb))
        rows = compare.diff_replay(ra, rb)
        origins = sorted({d.event.data_origin for d in ra.visible + rb.visible})
    elif query == "financials":
        cuts = (ra.financials.as_of, rb.financials.as_of)
        checks = (compare.financials_violations(ra), compare.financials_violations(rb))
        rows = compare.diff_financials(ra, rb)
        origins = [ra.financials.data_origin, rb.financials.data_origin]
    elif query == "prices":
        cuts = (ra.contract.as_of, rb.contract.as_of)
        checks = (
            compare.rows_violations(ra.rows, ra.contract.as_of, "bar"),
            compare.rows_violations(rb.rows, rb.contract.as_of, "bar"),
        )
        rows = compare.diff_rows(ra.rows, rb.rows, key="session_date", value="close", extra=("version",))
        origins = ra.data_origins + rb.data_origins
        st.markdown(f"**Price basis:** `{ra.contract.basis}` · point-in-time mode at both cutoffs")
    else:
        cuts = (ra.as_of, rb.as_of)
        checks = (
            compare.rows_violations(ra.observations, ra.as_of, "observation"),
            compare.rows_violations(rb.observations, rb.as_of, "observation"),
        )
        rows = compare.diff_rows(
            ra.observations, rb.observations, key="observation_date", value="value", extra=("vintage_date",)
        )
        origins = sorted({o.get("data_origin") or "unknown" for o in ra.observations + rb.observations})
    st.markdown(
        f"Cutoff **A**: {ui.fmt_dt(cuts[0])} · cutoff **B**: {ui.fmt_dt(cuts[1])} (as echoed by the API)"
    )
    if query == "prices" and any("synthetic" in o for o in origins):
        st.error("**SYNTHETIC PRICE DATA** — not market prices.")
    ui.origin_banner(origins or None)
    _verdict("A", checks[0])
    _verdict("B", checks[1])
    summary = compare.summarize(rows)
    cols = st.columns(max(len(summary), 1))
    for col, (k, v) in zip(cols, sorted(summary.items()), strict=False):
        col.metric(k, v)
    if not rows:
        st.info("Neither cutoff returned any records for this query (reported, not filled).")
        return
    st.caption(
        "Rows are aligned on their key only; values are shown exactly as returned at each cutoff. "
        "'only at B' = not yet available (or not yet ingested) at cutoff A."
    )
    ui.table(rows, height=400)


def show_replay(res: ReplayOut) -> None:
    st.markdown(
        f"Cutoff **{ui.fmt_dt(res.as_of)}** · window from {ui.fmt_dt(res.window_start)} · diagnostic "
        f"horizon to {ui.fmt_dt(res.horizon_end)}"
    )
    bad = integrity(res)
    if bad:
        st.error("Cutoff integrity violated:\n" + "\n".join(bad))
    else:
        st.success(
            "Cutoff integrity: every visible event has available_at ≤ cutoff; every excluded one is later."
        )
    withheld = [x for x in res.calendar if x.status == "withheld_observed_later"]
    cols = st.columns(4)
    cols[0].metric("Visible at cutoff", len(res.visible))
    cols[1].metric("Excluded: published later", len(res.excluded_future))
    cols[2].metric("Excluded: no availability time", len(res.excluded_no_availability_time))
    cols[3].metric("Calendar entries withheld", len(withheld))
    if res.visible:
        ui.origin_banner(sorted({d.event.data_origin for d in res.visible}))

    st.subheader("Availability precision of visible events")
    ui.table([{"precision": k, "events": v} for k, v in sorted(res.precision_counts.items())])

    def rows(ds) -> list[dict]:
        return [
            {
                "event_id": d.event.event_id,
                "available_at (NY)": ui.fmt_dt(d.event.available_at),
                "precision": str(d.event.availability_precision),
                "category": str(d.event.event_type),
                "title": d.event.title,
                "decision": d.reason,
                "masked links": ", ".join(f"{k}={v}" for k, v in d.masked_links.items()),
                "origin": d.event.data_origin,
            }
            for d in ds
        ]

    st.subheader("Events visible at the cutoff")
    ui.table(rows(res.visible), height=320)
    st.subheader("Events excluded because of later publication")
    st.warning(
        "Look-ahead diagnostic only: these events became available after the cutoff, so a run at the "
        "cutoff never sees them."
    )
    ui.table(rows(res.excluded_future), height=260)
    if res.excluded_no_availability_time:
        st.subheader("Excluded: no usable availability time")
        ui.table(rows(res.excluded_no_availability_time))

    st.subheader("Release-calendar coverage at the cutoff")
    st.caption(
        "known_* = a calendar snapshot containing the entry was retrieved by the cutoff. "
        "withheld_observed_later = first observed after the cutoff; historical calendars are not stored, "
        "so showing it would be look-ahead. known_due_unconfirmed = scheduled in the window but no linked "
        "publication was available by the cutoff (never reported as occurred)."
    )
    ui.table(
        [
            {
                "event_id": x.event.event_id,
                "scheduled_for (NY)": ui.fmt_dt(x.event.scheduled_for),
                "title": x.event.title,
                "status": x.status,
                "first observed (NY)": ui.fmt_dt(x.first_observed_at),
            }
            for x in res.calendar
        ]
    )
    if withheld:
        st.info(
            f"{len(withheld)} calendar entries were first observed after the cutoff: historical calendar "
            "coverage is missing for this cutoff."
        )
    with st.expander("How visibility is decided"):
        for n in res.notes:
            st.markdown(f"- {n}")
        st.markdown(
            "- Precision `EXACT_TIMESTAMP`: EDGAR acceptance time / Fed RSS pubDate.\n"
            "- `SCHEDULED_TIME_CONFIRMED_DATE`: official release time, date confirmed by ALFRED vintages.\n"
            "- `DATE_ONLY_CONSERVATIVE`: next day 00:00 New York.\n"
            "- `UNKNOWN`: never usable for replay."
        )
    ui.json_expander("Raw replay JSON", res)
