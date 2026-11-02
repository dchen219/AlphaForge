"""Page D — Historical Replay: what a run at a cutoff can see, and why (``GET /events/replay``)."""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from finance_agent.events.schemas import EventCategory
from finance_agent.interfaces.events_api import ReplayOut
from finance_agent.ui import components as ui
from finance_agent.ui import validation


def render() -> None:
    st.title("Historical Replay")
    st.caption(
        "Point-in-time visibility audit computed by the backend with the same store queries the "
        "briefing workflow uses. Visibility is never decided in the UI."
    )
    mode = ui.mode_selector("Snapshot / replay data")
    c, h = ui.backend(mode)
    if c is None:
        return
    ui.backend_caption(h, c)

    fixture_day = ui.fixture_cutoff_day(h)
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
    bad = [
        f"visible {d.event.event_id} has available_at {d.event.available_at} > cutoff"
        for d in res.visible
        if d.event.available_at is None or d.event.available_at > res.as_of
    ]
    bad += [
        f"excluded {d.event.event_id} has available_at {d.event.available_at} <= cutoff"
        for d in res.excluded_future
        if d.event.available_at is not None and d.event.available_at <= res.as_of
    ]
    return bad


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
