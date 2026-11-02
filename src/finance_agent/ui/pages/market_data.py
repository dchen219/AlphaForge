"""Market Data — prices, returns, Treasury yields, trading calendar and quality flags (``/market/*``).

Every number is rendered exactly as returned by the API; the price basis and query mode are always
displayed next to the data. Offline prices are SYNTHETIC and labelled as such on every view.
"""

from __future__ import annotations

from datetime import date, time, timedelta

import streamlit as st

from finance_agent.ui import components as ui
from finance_agent.ui import validation
from finance_agent.ui.client import PriceContract
from finance_agent.ui.labels import origin_label

BASES = {
    "raw": "raw — as traded (returns never span a split)",
    "split_adjusted": "split_adjusted — derived from stored splits",
    "total_return": "total_return — splits + reinvested cash dividends",
}
QUERY_MODES = {
    "point_in_time": "point_in_time — only data available at the cutoff",
    "research": "research — RETROSPECTIVE (all corporate actions known now)",
}
OFFLINE_RANGE = (date(2026, 1, 2), date(2026, 10, 30))  # inside the synthetic fixture window


def synthetic_banner(origins: list[str] | str | None) -> None:
    """Prices: the strongest label wins; synthetic bars are never 'market prices'."""
    lab = origin_label(origins)
    if lab.synthetic:
        st.error(
            "**SYNTHETIC PRICE DATA** — deterministic test bars in Alpaca format. These are NOT market "
            "prices and must not be read as live or historical quotes."
        )
    ui.origin_banner(origins)


def contract_banner(ct: PriceContract) -> None:
    st.markdown(
        f"**Price basis:** `{ct.basis}` · **adjustment method:** `{ct.adjustment_method or 'none (raw)'}` · "
        f"**query mode:** `{ct.query_mode}` · **as_of:** {ui.fmt_dt(ct.as_of) if ct.as_of else '—'}"
    )
    (st.warning if ct.retrospectively_adjusted or ct.query_mode == "research" else st.caption)(ct.statement)


def render() -> None:
    st.title("Market Data")
    st.caption(
        "Read-only queries over stored, versioned daily bars, corporate actions and ALFRED Treasury "
        "yields. Nothing is ingested from here; gaps are classified, never filled."
    )
    mode = ui.mode_selector()
    c, h = ui.backend(mode)
    if c is None:
        return
    ui.backend_caption(h, c)
    if mode == "offline":
        st.error(
            "Offline prices are **SYNTHETIC** (tests/fixtures/alpaca). Offline Treasury yields are **real** "
            "recorded ALFRED vintages."
        )
    try:
        inst = c.instruments()
    except Exception as exc:
        ui.show_error(exc, "Loading instruments")
        return

    tabs = st.tabs(
        ["Prices", "Returns", "Treasury yields", "Trading calendar & event timing", "Data quality"]
    )
    with tabs[0]:
        _prices(c, inst, mode)
    with tabs[1]:
        _returns(c, inst, mode)
    with tabs[2]:
        _yields(c, inst, mode)
    with tabs[3]:
        _calendar(c)
    with tabs[4]:
        _quality(c, inst)


def _default_range(mode: str) -> tuple[date, date]:
    if mode == "offline":
        return OFFLINE_RANGE
    end = date.today()
    return end - timedelta(days=180), end


def _query_inputs(key: str, inst, mode: str) -> dict | None:
    if not inst.instruments:
        st.info("No instruments stored. Ingest market data with the CLI first (see Dashboard).")
        return None
    ids = [i.instrument_id for i in inst.instruments]
    names = {i.instrument_id: f"{i.instrument_id} — {i.name} ({i.group})" for i in inst.instruments}
    col = st.columns([2, 2, 2])
    ticker = col[0].selectbox(
        "Instrument",
        ids,
        index=ids.index("NVDA") if "NVDA" in ids else 0,
        format_func=names.get,
        key=f"{key}_t",
    )
    basis = col[1].radio("Price basis (explicit)", list(BASES), format_func=BASES.get, key=f"{key}_basis")
    qmode = col[2].radio("Query mode", list(QUERY_MODES), format_func=QUERY_MODES.get, key=f"{key}_mode")
    d0, d1 = _default_range(mode)
    col = st.columns([1, 1, 2])
    start = col[0].date_input("Start", value=d0, key=f"{key}_start")
    end = col[1].date_input("End", value=d1, key=f"{key}_end")
    as_of = None
    with col[2]:
        if qmode == "point_in_time":
            as_of = ui.cutoff_input(
                "Point-in-time cutoff (default: now)",
                f"{key}_asof",
                help="Observations, corrections and corporate actions available after this are invisible.",
            )
        else:
            st.caption("Research mode is retrospective and takes no cutoff.")
    return {"ticker": ticker, "basis": basis, "mode": qmode, "start": start, "end": end, "as_of": as_of}


def _prices(c, inst, mode: str) -> None:
    q = _query_inputs("MP", inst, mode)
    if q is None:
        return
    provisional = st.checkbox(
        "Include PROVISIONAL bars (display only; never used in calculations)", key="MP_prov"
    )
    if st.button("Load prices", type="primary", key="MP_go"):
        try:
            a, b = validation.market_range(q["start"], q["end"])
            with st.spinner(f"GET /market/prices/{q['ticker']} …"):
                st.session_state["MP_res"] = c.prices(
                    q["ticker"], a.isoformat(), b.isoformat(), basis=q["basis"], mode=q["mode"],
                    as_of=q["as_of"], include_provisional=provisional,
                )  # fmt: skip
        except Exception as exc:
            ui.show_error(exc, "Loading prices")
    res = st.session_state.get("MP_res")
    if res is None:
        return
    st.subheader(f"{res.instrument_id} daily bars")
    synthetic_banner(res.data_origins)
    contract_banner(res.contract)
    bars = [r for r in res.rows if r.get("close") is not None]
    if bars:
        st.line_chart(
            [{"session": str(r["session_date"]), "close": r["close"]} for r in bars], x="session", y="close"
        )
        st.caption(f"Close on the `{res.contract.basis}` basis, as returned by the API (not recomputed).")
    flagged = [r for r in bars if r.get("quality_status") != "OK" or r.get("bar_status") != "FINAL"]
    cols = st.columns(4)
    cols[0].metric("Sessions", len(res.rows))
    cols[1].metric("Bars", len(bars))
    cols[2].metric("Missing sessions", len(res.missing_sessions))
    cols[3].metric("Flagged / provisional bars", len(flagged))
    if res.missing_sessions:
        st.warning("Missing sessions (classified, never filled):")
        ui.table([{"session": d, "classification": k} for d, k in sorted(res.missing_sessions.items())])
    for n in res.notes:
        st.info(n)
    if res.factors:
        with st.expander(f"Adjustment factors ({len(res.factors)})"):
            ui.table(res.factors)
    with st.expander("Bars (with version, availability and provenance)", expanded=False):
        cols_ = (
            "session_date", "open", "high", "low", "close", "volume", "price_factor", "bar_status",
            "quality_status", "version", "available_at", "availability_precision", "data_origin",
            "snapshot_sha256", "gap",
        )  # fmt: skip
        ui.table([{k: r.get(k) for k in cols_} for r in res.rows], height=360)


def _returns(c, inst, mode: str) -> None:
    q = _query_inputs("MR", inst, mode)
    if q is None:
        return
    col = st.columns(2)
    kind = col[0].radio("Return kind", ["simple", "log"], horizontal=True, key="MR_kind")
    window = col[1].number_input("Rolling volatility window (sessions)", 2, 252, 20, key="MR_win")
    if st.button("Compute returns (server-side)", type="primary", key="MR_go"):
        try:
            a, b = validation.market_range(q["start"], q["end"])
            with st.spinner(f"GET /market/returns/{q['ticker']} …"):
                st.session_state["MR_res"] = c.returns(
                    q["ticker"], a.isoformat(), b.isoformat(), basis=q["basis"], kind=kind,
                    mode=q["mode"], as_of=q["as_of"], window=int(window),
                )  # fmt: skip
        except Exception as exc:
            ui.show_error(exc, "Computing returns")
    res = st.session_state.get("MR_res")
    if res is None:
        return
    st.subheader(f"{res.instrument_id} {res.kind} returns")
    synthetic_banner(res.data_origins)
    contract_banner(res.contract)
    cols = st.columns(3)
    cols[0].metric("Cumulative return", _pct(res.cumulative_return))
    cols[1].metric("Max drawdown", _pct(res.max_drawdown))
    cols[2].metric("Current drawdown", _pct(res.drawdown_last))
    if res.cumulative_note:
        st.warning(res.cumulative_note)
    pts = [p for p in res.returns if p.get("value") is not None]
    if pts:
        st.line_chart([{"session": str(p["session_date"]), "return": p["value"]} for p in pts], x="session")
    blocked = [p for p in res.returns if p.get("value") is None]
    if blocked:
        st.warning(f"{len(blocked)} returns not computed (reason given by the API):")
        ui.table([{"session": p["session_date"], "reason": p.get("note")} for p in blocked])
    vol = res.rolling_volatility
    vpts = [p for p in vol.get("points", []) if p.get("value") is not None]
    st.markdown(
        f"**Rolling volatility** — window {vol.get('window')} sessions, annualized with "
        f"{vol.get('annualization')} trading days"
    )
    if vpts:
        st.line_chart(
            [{"session": str(p["session_date"]), "volatility": p["value"]} for p in vpts], x="session"
        )
    else:
        st.info("Not enough returns for the selected window.")
    for n in res.notes:
        st.info(n)


def _pct(v: float | None) -> str:
    return "not available" if v is None else f"{v * 100:.2f}%"


def _yields(c, inst, mode: str) -> None:
    series = list(inst.yield_series) or ["DGS10"]
    col = st.columns([2, 1, 1, 2])
    sid = col[0].selectbox(
        "Series", series, format_func=lambda s: f"{s} — {inst.yield_series.get(s, '')}", key="MY_sid"
    )
    d0, d1 = (date(2026, 9, 1), date(2026, 10, 30)) if mode == "offline" else _default_range(mode)
    start = col[1].date_input("Start", value=d0, key="MY_start")
    end = col[2].date_input("End", value=d1, key="MY_end")
    with col[3]:
        as_of = ui.cutoff_input("Vintage cutoff", "MY_asof", help="Only observations available by then.")
    if st.button("Load yields", type="primary", key="MY_go"):
        try:
            a, b = validation.market_range(start, end)
            with st.spinner(f"GET /market/yields/{sid} …"):
                st.session_state["MY_res"] = c.yields(sid, a.isoformat(), b.isoformat(), as_of)
        except Exception as exc:
            ui.show_error(exc, "Loading yields")
    res = st.session_state.get("MY_res")
    if res is None:
        return
    st.subheader(f"{res.series_id} — {res.title}")
    ui.origin_banner(sorted({o.get("data_origin") or "unknown" for o in res.observations}) or None)
    st.caption(f"Units: {res.units}. Yields are rates, not prices.")
    vals = [o for o in res.observations if o.get("value") is not None]
    if vals:
        st.line_chart([{"date": str(o["observation_date"]), "yield (%)": o["value"]} for o in vals], x="date")
    ch = [x for x in res.changes if x.get("change_bp") is not None]
    if ch:
        st.bar_chart([{"date": str(x.get("date")), "change (bp)": x["change_bp"]} for x in ch], x="date")
    nulls = len(res.observations) - len(vals)
    if nulls:
        st.warning(f"{nulls} observations are null (holiday / not published) — reported, never filled.")
    with st.expander(f"Observations ({len(res.observations)}) with vintage and availability"):
        ui.table(res.observations, height=320)
    for n in res.notes:
        st.caption(n)


def _calendar(c) -> None:
    st.markdown("**Session lookup** — XNYS via `exchange_calendars`")
    col = st.columns(3)
    day = col[0].date_input("Date", value=date(2026, 11, 26), key="MC_day")
    start = col[1].date_input("Range start", value=date(2026, 11, 20), key="MC_start")
    end = col[2].date_input("Range end", value=date(2026, 12, 5), key="MC_end")
    st.markdown("**Event timing alignment** — which session does an event's timestamp map to?")
    col = st.columns(3)
    ev_day = col[0].date_input("Event date (New York)", value=date(2026, 10, 29), key="MC_evd")
    ev_time = col[1].time_input("Event time (New York)", value=time(14, 0), key="MC_evt")
    ev_id = col[2].text_input("…or an event id (uses its available_at)", key="MC_evid", max_chars=19)
    if st.button("Inspect calendar", type="primary", key="MC_go"):
        try:
            validation.market_range(start, end, max_days=400)
            if ev_id.strip():
                detail = c.event(validation.event_id(ev_id))
                if detail.event.available_at is None:
                    raise validation.ValidationError("That event has no available_at; it cannot be aligned.")
                event_time = detail.event.available_at.isoformat()
            else:
                event_time = validation.cutoff(ev_day, ev_time)
            with st.spinner("GET /market/calendar …"):
                st.session_state["MC_res"] = c.market_calendar(
                    day=day.isoformat(), start=start.isoformat(), end=end.isoformat(), event_time=event_time
                )
        except Exception as exc:
            ui.show_error(exc, "Calendar lookup")
    res = st.session_state.get("MC_res")
    if res is None:
        return
    st.caption(f"Calendar `{res.calendar}` · source {res.source}")
    cols = st.columns(3)
    cols[0].metric(f"{res.date} is a session", "yes" if res.is_session else "no")
    cols[1].metric("Previous session", str(res.previous_session))
    cols[2].metric("Next session", str(res.next_session))
    if res.session:
        st.json(res.session, expanded=False)
    if res.alignment:
        st.markdown("**Event alignment** (computed by the backend)")
        st.json(res.alignment)
    if res.sessions is not None:
        with st.expander(f"Sessions in range ({len(res.sessions)})", expanded=True):
            ui.table(res.sessions)


def _quality(c, inst) -> None:
    ids = ["(all)"] + [i.instrument_id for i in inst.instruments]
    col = st.columns(2)
    t = col[0].selectbox("Instrument", ids, key="MQ_t")
    sev = col[1].selectbox("Severity", ["(all)", "INFO", "WARNING", "ERROR"], key="MQ_sev")
    if st.button("Load quality flags", type="primary", key="MQ_go"):
        try:
            with st.spinner("GET /market/quality …"):
                st.session_state["MQ_res"] = c.quality(
                    ticker=None if t == "(all)" else t, severity=None if sev == "(all)" else sev
                )
        except Exception as exc:
            ui.show_error(exc, "Loading quality flags")
    res = st.session_state.get("MQ_res")
    if res is None:
        return
    st.caption("Extreme moves are flagged, never deleted; quarantined bars are excluded from calculations.")
    ui.table([{"check": k, "issues": v} for k, v in sorted(res.summary.items())])
    st.markdown(f"**Issues ({res.count})**")
    ui.table(res.issues, height=320)
    with st.expander(f"Recent ingestion runs ({len(res.runs)})"):
        ui.table(res.runs)
