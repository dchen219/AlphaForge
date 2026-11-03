"""Page B — Financial Events Explorer (v0.2): point-in-time event queries over the event database."""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from finance_agent.events.schemas import EventCategory, EventSubtype, FinancialEvent, ImportanceLevel
from finance_agent.interfaces.events_api import EventDetail
from finance_agent.ui import components as ui
from finance_agent.ui import validation

SOURCES = (
    "SEC EDGAR",
    "SEC EDGAR XBRL",
    "Federal Reserve",
    "BLS via ALFRED",
    "BEA via ALFRED",
)


def event_row(e: FinancialEvent) -> dict:
    return {
        "event_id": e.event_id,
        "available_at (NY)": ui.fmt_dt(e.available_at),
        "precision": str(e.availability_precision),
        "category": str(e.event_type),
        "subtype": str(e.event_subtype),
        "title": e.title,
        "importance": str(e.importance_level or "—"),
        "score": e.importance_score,
        "verification": str(e.verification_status),
        "status": str(e.event_status),
        "origin": e.data_origin,
        "source_url": e.source_url,
    }


def default_range(h) -> tuple[date, date]:
    end = ui.fixture_cutoff_day(h) or date.today()
    return end - timedelta(days=60 if h.fixture_event_window else 30), end


def render() -> None:
    st.title("Financial Events Explorer")
    st.caption(
        "Reads the existing event database (`GET /events`, `/events/{id}`, `/events/calendar`). "
        "Nothing here ingests. Populate it with `finance-agent events ingest`."
    )
    mode = ui.mode_selector()
    c, h = ui.backend(mode)
    if c is None:
        return
    ui.backend_caption(h, c)

    d0, d1 = default_range(h)
    col = st.columns(3)
    rng = col[0].date_input("Available between (New York dates)", value=(d0, d1), key="B_range")
    source = col[1].selectbox("Source", ["(any)", *SOURCES], key="B_source")
    raw_ticker = col[2].text_input("Company ticker (optional)", key="B_ticker", max_chars=10)
    col = st.columns(3)
    cats = col[0].multiselect("Event category", [x.value for x in EventCategory], key="B_cat")
    subs = col[1].multiselect("Event subtype", [x.value for x in EventSubtype], key="B_sub")
    levels = col[2].multiselect("Importance level", [x.value for x in ImportanceLevel], key="B_lvl")
    as_of = ui.cutoff_input(
        "Historical cutoff (as_of)",
        "B_asof",
        help="Hide everything that became available after this instant; later links are masked.",
    )
    limit = st.slider("Max events", 10, 1000, 200, step=10, key="B_limit")

    if st.button("Search events", type="primary"):
        try:
            if not isinstance(rng, tuple) or len(rng) != 2:
                raise validation.ValidationError("Select both a start and an end date.")
            start, end = validation.date_range(*rng)
            ticker = validation.ticker(raw_ticker, required=False)
            with st.spinner("GET /events …"):
                st.session_state["B_events"] = c.events(
                    start=start.isoformat(),
                    end=end.isoformat(),
                    as_of=as_of,
                    categories=cats,
                    subtypes=subs,
                    ticker=ticker,
                    levels=levels,
                    source=None if source == "(any)" else source,
                    limit=limit,
                )
                st.session_state["B_calendar"] = c.calendar(start.isoformat(), end.isoformat())
                st.session_state["B_query_asof"] = as_of
        except Exception as exc:
            ui.show_error(exc, "Event search")

    result = st.session_state.get("B_events")
    if result is None:
        st.info("Set filters and click **Search events**.")
        return
    cutoff = st.session_state.get("B_query_asof")
    st.subheader(f"{result.count} occurred events" + (f" visible at {cutoff}" if cutoff else ""))
    if result.events:
        ui.origin_banner(sorted({e.data_origin for e in result.events}))
    ui.table([event_row(e) for e in result.events], link_columns=("source_url",), height=380)

    cal = st.session_state.get("B_calendar")
    if cal is not None:
        with st.expander(f"Release calendar in range — SCHEDULED, not occurred ({cal.count})"):
            st.caption(
                "Calendar entries are never occurrences. This listing is not filtered by cutoff; the "
                "Historical Replay page shows which entries were known at a cutoff."
            )
            ui.table(
                [
                    {
                        "event_id": e.event_id,
                        "scheduled_for (NY)": ui.fmt_dt(e.scheduled_for),
                        "title": e.title,
                        "status": str(e.event_status),
                        "source": e.source_name,
                        "source_url": e.source_url,
                    }
                    for e in cal.events
                ],
                link_columns=("source_url",),
            )

    if not result.events:
        return
    st.subheader("Event details")
    ids = [e.event_id for e in result.events]
    titles = {e.event_id: e.title for e in result.events}
    eid = st.selectbox("Event", ids, format_func=lambda i: f"{i} — {titles[i][:90]}", key="B_pick")
    try:
        detail = c.event(validation.event_id(eid), as_of=cutoff)
    except Exception as exc:
        ui.show_error(exc, "Loading event detail")
        return
    show_detail(detail)


def show_detail(d: EventDetail) -> None:
    e = d.event
    ui.origin_banner(e.data_origin)
    st.markdown(f"### {e.title}")
    if e.description:
        st.write(e.description)
    if d.as_of:
        st.caption(f"Point-in-time view at {ui.fmt_dt(d.as_of)}: links to later information are hidden.")
    cols = st.columns(4)
    cols[0].metric("Importance", str(e.importance_level or "—"))
    cols[1].metric("Score", "—" if e.importance_score is None else f"{e.importance_score:.1f}")
    cols[2].metric("Verification", str(e.verification_status))
    cols[3].metric("Status", str(e.event_status))

    st.markdown("**Timing**")
    ui.table(
        [
            {"field": k, "value (New York)": ui.fmt_dt(getattr(e, k)), "UTC": str(getattr(e, k) or "—")}
            for k in ("occurred_at", "scheduled_for", "published_at", "available_at", "ingested_at")
        ]
        + [{"field": "availability_precision", "value (New York)": str(e.availability_precision), "UTC": ""}]
    )
    if e.value is not None or e.previous_value is not None:
        st.markdown("**Reported values**")
        ui.table(
            [
                {
                    "series": e.series_id,
                    "period": e.observation_period,
                    "value": e.value,
                    "previous": e.previous_value,
                    "unit": str(e.unit or ""),
                    "consensus": str(e.consensus_status),
                    "revision_status": str(e.revision_status),
                }
            ]
        )
        if str(e.consensus_status) != "available":
            st.caption("No authenticated consensus source: no surprise is computed.")

    st.markdown("**Importance breakdown** (judgmental weights, `importance-v0.2.1`)")
    comps = e.importance_components or {}
    ui.table(
        [
            {
                "component": k,
                "value": v.get("value"),
                "max": v.get("max"),
                "available": v.get("available"),
                "explanation": v.get("explanation"),
            }
            for k, v in comps.items()
            if isinstance(v, dict)
        ]
    )
    if "partial_score" in comps:
        st.warning(f"INSUFFICIENT_DATA: partial score {comps['partial_score']} is not used for ranking.")
    if comps.get("formula"):
        st.caption(f"{comps.get('method', '')}: {comps['formula']}")

    if e.comparisons:
        st.markdown("**Deterministic comparisons**")
        ui.table(
            [
                {
                    "label": x.label,
                    "value": x.value,
                    "unit": str(x.unit),
                    "formula": x.formula,
                    "status": x.status,
                    "note": x.note,
                }
                for x in e.comparisons
            ]
        )
    if e.channels:
        with st.expander(f"Economic channels — labelled hypotheses, no coefficients ({len(e.channels)})"):
            ui.table(
                [
                    {
                        "source → target": f"{x.source_variable} → {x.target_variable}",
                        "direction": x.direction,
                        "type": x.interpretation_type,
                        "mechanism": x.mechanism,
                        "basis": x.confidence_basis,
                    }
                    for x in e.channels
                ]
            )

    st.markdown("**Sources**")
    ui.table(
        [
            {
                "source": s.source_name,
                "role": s.role,
                "url": s.source_url,
                "retrieved_at (NY)": ui.fmt_dt(s.retrieved_at),
                "sha256": (s.snapshot_sha256 or "")[:16],
                "excerpt": (s.excerpt or "")[:200],
            }
            for s in d.sources
        ],
        link_columns=("url",),
    )
    st.markdown("**Related events**")
    st.write(
        {
            "cluster": e.event_cluster_id,
            "cluster_members": d.cluster_members,
            "related_event_id": e.related_event_id,
            "supersedes_event_id": e.supersedes_event_id,
            "affected_entities": e.affected_entities,
        }
    )
    if d.revision_history:
        st.markdown("**Revision history** (first release preserved; revisions are new events)")
        ui.table(
            [
                {
                    "event_id": r.event_id,
                    "available_at (NY)": ui.fmt_dt(r.available_at),
                    "status": str(r.event_status),
                    "revision": str(r.revision_status),
                    "value": r.value,
                    "supersedes": r.supersedes_event_id,
                }
                for r in d.revision_history
            ]
        )
    else:
        st.caption("No revision history linked to this event" + (" at this cutoff." if d.as_of else "."))
    ui.json_expander("Raw event JSON", d)
