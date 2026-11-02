"""Page C — Daily Briefing (v0.2): run the bounded briefing workflow over stored events."""

from __future__ import annotations

from datetime import date, timedelta

import streamlit as st

from finance_agent.events.briefing import BriefingEntry
from finance_agent.interfaces.events_api import BriefingRunOut
from finance_agent.ui import components as ui
from finance_agent.ui import validation


def render() -> None:
    st.title("Daily Briefing")
    st.caption(
        "`POST /intelligence/briefing` runs the LangGraph briefing workflow over the existing event "
        "database (no ingestion from the UI). Works fully without an LLM."
    )
    mode = ui.mode_selector()
    c, h = ui.backend(mode)
    if c is None:
        return
    ui.backend_caption(h, c)

    default_day = ui.fixture_cutoff_day(h) or date.today()
    while default_day.weekday() >= 5:  # default to the latest weekday (releases are weekday events)
        default_day -= timedelta(days=1)
    if h.fixture_event_window:
        st.caption(
            f"Offline fixtures were recorded over {h.fixture_event_window['start']} → "
            f"{h.fixture_event_window['as_of']}; briefings outside that window will be empty."
        )
    gen, stored = st.tabs(["Generate briefing", "Retrieve stored briefing"])
    with gen:
        col = st.columns(3)
        day = col[0].date_input("Briefing date (New York)", value=default_day, key="C_date")
        window_hours = col[1].number_input(
            "Look-back window (hours, 0 = the calendar day)", 0, 744, 0, key="C_window"
        )
        use_llm = col[2].toggle("LLM narrative", value=False, key="C_llm")
        cutoff = ui.cutoff_input(
            "Explicit analysis cutoff (overrides date)",
            "C_asof",
            help="Unchecked: cutoff = 23:59:59 New York on the briefing date.",
        )
        col = st.columns(3)
        max_macro = col[0].number_input("Max macro clusters", 1, 25, 6, key="C_mm")
        max_corp = col[1].number_input("Max corporate clusters", 0, 50, 8, key="C_mc")
        raw_tickers = col[2].text_input("Tickers (comma-separated, optional)", key="C_tickers")
        if st.button("Generate briefing", type="primary"):
            try:
                tickers = [validation.ticker(t) for t in raw_tickers.split(",") if t.strip()] or None
                # A timed cutoff is an explicit as_of; a date-only cutoff keeps the date rule
                # (23:59:59 New York); otherwise the briefing date defines the cutoff.
                timed = cutoff is not None and "T" in cutoff
                with st.spinner("Running briefing workflow …"):
                    st.session_state["C_run"] = c.briefing(
                        date=None if timed else (cutoff or day.isoformat()),
                        as_of=cutoff if timed else None,
                        window_hours=int(window_hours) or None,
                        max_macro=int(max_macro),
                        max_corporate=int(max_corp),
                        tickers=tickers,
                        use_llm=use_llm,
                    )
            except Exception as exc:
                ui.show_error(exc, "Briefing generation")
    with stored:
        try:
            listing = c.briefings(limit=50)
        except Exception as exc:
            ui.show_error(exc, "Listing briefings")
            listing = None
        if listing is not None and listing.briefings:
            opts = {b.briefing_id: b for b in listing.briefings}
            bid = st.selectbox(
                "Stored briefings (most recent first)",
                list(opts),
                format_func=lambda i: (
                    f"{ui.fmt_dt(opts[i].as_of)} · {opts[i].status} · "
                    f"{opts[i].verification_status or '—'} · {i[:8]}"
                ),
                key="C_pick",
            )
            if st.button("Load briefing"):
                try:
                    st.session_state["C_run"] = c.briefing_run(validation.run_id(bid))
                except Exception as exc:
                    ui.show_error(exc, "Loading briefing")
        elif listing is not None:
            st.info("No stored briefings yet.")

    run = st.session_state.get("C_run")
    if run is not None:
        show_briefing(run)


def _entry(e: BriefingEntry) -> None:
    head = f"#{e.rank} {e.title} — {e.importance_level or '—'}"
    if e.importance_score is not None:
        head += f" ({e.importance_score:.1f})"
    with st.expander(head, expanded=e.rank <= 2):
        st.markdown(f"**What happened:** {e.what_happened}")
        st.markdown(f"**When:** {e.when}")
        st.markdown(f"**Reported:** {e.what_was_reported}")
        if e.what_changed:
            st.markdown("**What changed:**\n" + "\n".join(f"- {x}" for x in e.what_changed))
        if e.why_it_matters:
            st.markdown(
                "**Why it may matter (hypotheses):**\n" + "\n".join(f"- {x}" for x in e.why_it_matters)
            )
        if e.importance_explanation:
            st.caption("Importance: " + " · ".join(e.importance_explanation))
        st.caption(
            f"Origin `{e.data_origin}` · evidence {', '.join(e.evidence_ids)} · events {', '.join(e.event_ids)}"
        )
        for u in e.source_urls:
            st.markdown(f"- [{u}]({u})")
        for n in e.notes:
            st.caption(n)


def show_briefing(run: BriefingRunOut) -> None:
    st.divider()
    st.markdown(f"**Briefing** `{run.briefing_id}` · status **{run.status}**")
    if run.error:
        st.error(f"Workflow error: {run.error}")
    rep = run.report
    if rep is None:
        ui.table(run.steps)
        return
    st.caption(
        f"Window {ui.fmt_dt(rep.window_start)} → {ui.fmt_dt(rep.window_end)} (cutoff) · generated "
        f"{ui.fmt_dt(rep.generated_at)}"
    )
    ui.origin_banner(rep.data_origins)
    ui.verification_banner(rep.verification.status, rep.verification.checks)
    llm = rep.llm or {}
    (st.success if llm.get("status") == "generated" else st.info)(
        f"**LLM:** provider `{llm.get('provider')}`, status **{llm.get('status')}**"
        + ("" if llm.get("status") == "generated" else " — deterministic template narrative.")
    )
    for n in llm.get("notes", []):
        st.caption(f"LLM note: {n}")

    st.subheader("Executive summary")
    st.write(rep.executive_summary)
    if rep.model_executive_summary:
        st.markdown("**Model summary** — `model_interpretation` (screened; contains no figures)")
        st.write(rep.model_executive_summary)
    cols = st.columns(len(rep.counts) or 1)
    for col, (k, v) in zip(cols, rep.counts.items(), strict=False):
        col.metric(k.replace("_", " "), v)

    st.subheader(f"Ranked macro events ({len(rep.macro_entries)})")
    for e in rep.macro_entries:
        _entry(e)
    if not rep.macro_entries:
        st.info("No macro events became available in this window.")
    st.subheader(f"Corporate disclosures ({len(rep.corporate_entries)})")
    for e in rep.corporate_entries:
        _entry(e)
    if not rep.corporate_entries:
        st.info("No corporate disclosures became available in this window.")

    st.subheader("Economic implications (hypotheses)")
    for x in rep.implications or ["None derived."]:
        st.markdown(f"- {x}")
    if rep.model_interpretations:
        st.markdown("**Model interpretations** — `model_interpretation`")
        for it in rep.model_interpretations:
            st.markdown(
                f"- {it.get('text')}  \n  <small>events: {', '.join(it.get('event_ids', []))}</small>",
                unsafe_allow_html=True,
            )
    if rep.affected_markets:
        st.caption("Affected: " + ", ".join(rep.affected_markets))

    st.subheader("Freshness, coverage and uncertainty")
    for u in rep.uncertainty:
        st.warning(u)
    if rep.upcoming:
        with st.expander(f"Upcoming scheduled releases ({len(rep.upcoming)}) — not occurred"):
            ui.table(rep.upcoming)
    with st.expander(f"Evidence / sources ({len(rep.sources)})"):
        ui.table(
            [
                {
                    "event_id": s.get("event_id"),
                    "source": s.get("source_name"),
                    "role": s.get("role"),
                    "url": s.get("source_url"),
                    "retrieved_at": s.get("retrieved_at"),
                    "origin": s.get("data_origin"),
                }
                for s in rep.sources
            ],
            link_columns=("url",),
        )
    with st.expander("Briefing (Markdown)"):
        st.markdown(rep.markdown)
    ui.json_expander("Raw briefing JSON", run)
