"""Agent Testing Lab: execute workflows, inspect stored run records, compare runs."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

import streamlit as st

from finance_agent.interfaces.events_api import BriefingRunOut
from finance_agent.schemas.research import ResearchRunOut
from finance_agent.ui import components as ui
from finance_agent.ui import validation

UNAVAILABLE = "not recorded"


def node_rows(planned: list[str], steps: list[dict]) -> list[dict]:
    """Planned graph nodes joined with recorded steps; nothing is inferred for unrecorded nodes."""
    by_name = {s.get("step"): s for s in steps}
    rows = []
    for name in planned + [s.get("step") for s in steps if s.get("step") not in planned]:
        s = by_name.get(name)
        rows.append(
            {
                "node": name,
                "executed": "yes" if s else "no",
                "status": s.get("status") if s else "not executed",
                "started_at": (s.get("started_at") or UNAVAILABLE) if s else "—",
                "finished_at": (s.get("finished_at") or UNAVAILABLE) if s else "—",
                "duration_ms (measured)": str(s["ms"]) if s and "ms" in s else (UNAVAILABLE if s else "—"),
                "error": s.get("error", "") if s else "",
            }
        )
    return rows


def _duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return UNAVAILABLE
    return f"{(end - start).total_seconds():.2f} s (from stored created/completed timestamps)"


def render() -> None:
    st.title("Agent Testing Lab")
    st.caption(
        "Execute the existing LangGraph workflows, then inspect and compare their stored run records "
        "(`POST /research/company`, `POST /intelligence/briefing`, `GET /research`, "
        "`/intelligence/briefings`). Only what the workflows record is shown: tool-call traces, token "
        "usage and agent memory are not exposed by the backend and are not invented here."
    )
    mode = ui.mode_selector()
    c, h = ui.backend(mode)
    if c is None:
        return
    ui.backend_caption(h, c)
    try:
        graphs = c.workflows()
    except Exception as exc:
        ui.show_error(exc, "Loading workflow definitions")
        return
    kind = st.radio("Workflow", list(graphs), horizontal=True, key="E_kind")
    planned = graphs.get(kind, [])
    st.markdown(f"**Planned graph:** {' → '.join(planned)} (any node error → `fail`)")
    run_tab, inspect_tab, compare_tab = st.tabs(["Execute", "Inspect stored runs", "Compare runs"])
    with run_tab:
        _execute(c, h, kind, planned)
    with inspect_tab:
        _inspect(c, kind, planned)
    with compare_tab:
        _compare(c, kind)


def _execute(c, h, kind: str, planned: list[str]) -> None:
    if kind == "company_research":
        col = st.columns(3)
        raw_ticker = col[0].text_input("Ticker", value="MSFT", max_chars=10, key="EX_ticker")
        n_q = col[1].number_input("Quarters", 2, 20, 8, key="EX_q")
        use_llm = col[2].toggle("LLM interpretation", value=False, key="EX_llm")
        question = st.text_input(
            "Question (optional; deterministic parsing first)", max_chars=2000, key="EX_question"
        )
        as_of = ui.cutoff_input("As-of cutoff", "EX_asof")
    else:
        default_day = ui.fixture_cutoff_day(h) or date.today()
        while default_day.weekday() >= 5:
            default_day -= timedelta(days=1)
        col = st.columns(3)
        day = col[0].date_input("Briefing date (New York)", value=default_day, key="EX_day")
        raw_tickers = col[1].text_input("Tickers (comma-separated, optional)", key="EX_tickers")
        use_llm = col[2].toggle("LLM narrative", value=False, key="EX_bllm")
    if use_llm and not (h.credentials_configured.get("anthropic_api_key") and h.llm_provider != "none"):
        st.info(f"LLM provider is `{h.llm_provider}`: the run will record the deterministic fallback.")
    if st.button("Execute workflow", type="primary", key="EX_go"):
        t0 = time.perf_counter()
        try:
            with st.spinner(f"Running `{kind}` …"):
                if kind == "company_research":
                    run = c.research(
                        validation.ticker(raw_ticker),
                        validation.quarters(n_q),
                        as_of,
                        use_llm,
                        question.strip() or None,
                    )
                else:
                    tickers = [validation.ticker(t) for t in raw_tickers.split(",") if t.strip()] or None
                    run = c.briefing(date=day.isoformat(), tickers=tickers, use_llm=use_llm)
            st.session_state["EX_result"] = (kind, run, time.perf_counter() - t0, None)
        except Exception as exc:
            st.session_state["EX_result"] = (kind, None, time.perf_counter() - t0, exc)
    res = st.session_state.get("EX_result")
    if res is None or res[0] != kind:
        st.info("Configure inputs and click **Execute workflow**.")
        return
    _, run, elapsed, exc = res
    st.metric("Round-trip time (measured by this dashboard)", f"{elapsed:.2f} s")
    st.caption("Wall-clock time of the HTTP call, including server-side work; recorded per-node times below.")
    if exc is not None:
        ui.show_error(exc, f"Executing {kind}")
        return
    if kind == "company_research":
        from finance_agent.ui.pages.company_research import _report

        _research(run, planned)
        if run.report is not None:
            st.divider()
            st.subheader("Structured output")
            _report(run)
    else:
        from finance_agent.ui.pages.daily_briefing import show_briefing

        _briefing(run, planned)
        st.subheader("Structured output")
        show_briefing(run)


def _listing(c, kind: str) -> tuple[list[dict], list[str], dict[str, str]]:
    if kind == "company_research":
        listing = c.research_runs(limit=100)
        rows = [
            {
                "run_id": r.run_id,
                "status": r.status,
                "ticker": r.ticker,
                "created (NY)": ui.fmt_dt(r.created_at),
                "completed (NY)": ui.fmt_dt(r.completed_at),
                "as_of": ui.fmt_dt(r.as_of) if r.as_of else "latest",
                "use_llm": r.use_llm,
                "verification": r.verification_status,
                "nodes": r.steps_executed,
                "error": (r.error or "")[:120],
            }
            for r in listing.runs
        ]
        names = {
            r.run_id: f"{r.ticker} · {ui.fmt_dt(r.created_at)} · {r.status} · {r.run_id[:8]}"
            for r in listing.runs
        }
        return rows, [r.run_id for r in listing.runs], names
    blist = c.briefings(limit=100)
    rows = [
        {
            "briefing_id": b.briefing_id,
            "status": b.status,
            "cutoff (NY)": ui.fmt_dt(b.as_of),
            "created (NY)": ui.fmt_dt(b.created_at),
            "completed (NY)": ui.fmt_dt(b.completed_at),
            "verification": b.verification_status,
            "llm": b.llm_status,
            "origins": ", ".join(b.data_origins),
            "error": (b.error or "")[:120],
        }
        for b in blist.briefings
    ]
    names = {
        b.briefing_id: f"cutoff {ui.fmt_dt(b.as_of)} · {b.status} · {b.briefing_id[:8]}"
        for b in blist.briefings
    }
    return rows, [b.briefing_id for b in blist.briefings], names


def _inspect(c, kind: str, planned: list[str]) -> None:
    try:
        rows, ids, names = _listing(c, kind)
    except Exception as exc:
        ui.show_error(exc, "Loading run records")
        return
    st.subheader(f"Stored runs ({len(rows)})")
    ui.table(rows, height=260)
    if not ids:
        st.info("No runs recorded yet. Execute a workflow first.")
        return
    rid = st.selectbox("Inspect run", ids, format_func=names.get, key="E_pick")
    try:
        validation.run_id(rid)
        if kind == "company_research":
            _research(c.research_run(rid), planned)
        else:
            _briefing(c.briefing_run(rid), planned)
    except Exception as exc:
        ui.show_error(exc, "Loading run")


def _compare(c, kind: str) -> None:
    try:
        _, ids, names = _listing(c, kind)
    except Exception as exc:
        ui.show_error(exc, "Loading run records")
        return
    if len(ids) < 2:
        st.info("At least two stored runs of this workflow are needed for a comparison.")
        return
    col = st.columns(2)
    a = col[0].selectbox("Run A", ids, index=1, format_func=names.get, key="CMP_a")
    b = col[1].selectbox("Run B", ids, index=0, format_func=names.get, key="CMP_b")
    if st.button("Compare", type="primary", key="CMP_go"):
        try:
            if kind == "company_research":
                pair = (c.research_run(validation.run_id(a)), c.research_run(validation.run_id(b)))
            else:
                pair = (c.briefing_run(validation.run_id(a)), c.briefing_run(validation.run_id(b)))
            st.session_state["CMP_res"] = (kind, *pair)
        except Exception as exc:
            ui.show_error(exc, "Loading runs")
    res = st.session_state.get("CMP_res")
    if res is None or res[0] != kind:
        return
    _, ra, rb = res
    st.subheader("Run summary")
    ui.table(compare_summary(kind, ra, rb))
    if kind == "company_research":
        st.subheader("Reported values by metric and period")
        st.caption(
            "Values exactly as stored in each report (no recalculation). 'differs' marks a different value "
            "or a value present in only one run — e.g. different cutoffs, restatements or coverage."
        )
        ui.table(compare_metric_values(ra, rb), height=360)
    else:
        st.subheader("Selected event clusters")
        ui.table(compare_briefing_events(ra, rb), height=320)


def _status(run) -> tuple[str, str, str]:
    rep = run.report
    if rep is None:
        return run.status, "not available", "not available"
    llm = rep.llm.status if hasattr(rep.llm, "status") else (rep.llm or {}).get("status")
    return run.status, rep.verification.status, str(llm)


def compare_summary(kind: str, ra, rb) -> list[dict]:
    """Side-by-side facts of two stored runs (values copied from the records, never derived)."""
    (sa, va, la), (sb, vb, lb) = _status(ra), _status(rb)
    pa, pb = ra.report, rb.report
    fields = [
        ("status", sa, sb),
        ("verification", va, vb),
        ("llm status", la, lb),
        ("steps recorded", len(ra.steps), len(rb.steps)),
        ("error", ra.error or "—", rb.error or "—"),
    ]
    if kind == "company_research":
        fields += [
            ("ticker", ra.ticker, rb.ticker),
            ("as_of", _asof(pa), _asof(pb)),
            ("data origin", pa.data_origin if pa else "—", pb.data_origin if pb else "—"),
            ("metric values", len(pa.metric_values) if pa else 0, len(pb.metric_values) if pb else 0),
            ("missing values", len(pa.missing_data) if pa else 0, len(pb.missing_data) if pb else 0),
        ]
    else:
        fields += [
            ("cutoff", ui.fmt_dt(ra.as_of), ui.fmt_dt(rb.as_of)),
            (
                "data origins",
                ", ".join(pa.data_origins) if pa else "—",
                ", ".join(pb.data_origins) if pb else "—",
            ),
        ]
    return [
        {"field": f, "run A": str(x), "run B": str(y), "differs": "yes" if str(x) != str(y) else ""}
        for f, x, y in fields
    ]


def _asof(rep) -> str:
    if rep is None:
        return "—"
    return ui.fmt_dt(rep.as_of) if rep.as_of else "latest"


def compare_metric_values(ra, rb) -> list[dict]:
    def index(run) -> dict:
        return {(v.metric, v.period.label): v for v in run.report.metric_values} if run.report else {}

    ia, ib = index(ra), index(rb)
    rows = []
    for key in sorted(set(ia) | set(ib)):
        va, vb = ia.get(key), ib.get(key)
        differs = (va is None) != (vb is None) or (va is not None and vb is not None and va.value != vb.value)
        rows.append(
            {
                "metric": key[0],
                "period": key[1],
                "run A": va.value if va else None,
                "run B": vb.value if vb else None,
                "unit": (va or vb).unit,
                "restated (A/B)": f"{va.restated if va else '—'}/{vb.restated if vb else '—'}",
                "differs": "yes" if differs else "",
            }
        )
    return rows


def compare_briefing_events(ra, rb) -> list[dict]:
    def entries(run) -> dict:
        rep = run.report
        return {e.cluster_id: e for e in rep.macro_entries + rep.corporate_entries} if rep else {}

    ea, eb = entries(ra), entries(rb)
    return [
        {
            "cluster": k,
            "title": (ea.get(k) or eb.get(k)).title,
            "rank A": ea[k].rank if k in ea else "—",
            "rank B": eb[k].rank if k in eb else "—",
            "in": "both" if k in ea and k in eb else ("A only" if k in ea else "B only"),
        }
        for k in sorted(set(ea) | set(eb))
    ]


def _header(kind: str, run_id: str, status: str, start, end, error: str | None) -> None:
    cols = st.columns(4)
    cols[0].metric("Workflow", kind)
    cols[1].metric("Status", status)
    cols[2].metric("Started (NY)", ui.fmt_dt(start))
    cols[3].metric("Ended (NY)", ui.fmt_dt(end))
    st.caption(f"Run ID `{run_id}` · total duration: {_duration(start, end)}")
    if error:
        st.error(f"Error: {error}")


def _research(run: ResearchRunOut, planned: list[str]) -> None:
    _header("company_research", run.run_id, run.status, run.created_at, run.completed_at, run.error)
    st.subheader("Nodes")
    ui.table(node_rows(planned, run.steps))
    st.caption(
        "Per-node start/finish timestamps exist only for runs recorded after this console was added; "
        "older runs show 'not recorded'. Individual tool/HTTP calls and LLM token usage are not traced."
    )
    st.markdown(f"**Request parameters:** `{run.params or UNAVAILABLE}`")
    rep = run.report
    if rep is None:
        st.warning(
            "No report persisted (run failed or still running): providers and verification unavailable."
        )
        return
    st.subheader("Data providers and provenance")
    ui.origin_banner(rep.data_origin)
    ui.table(
        [
            {
                "provider host": urlsplit(s.url).hostname,
                "url": s.url,
                "origin": s.origin,
                "retrieved_at (NY)": ui.fmt_dt(s.retrieved_at),
                "sha256": s.sha256,
            }
            for s in rep.snapshots
        ],
        link_columns=("url",),
    )
    st.subheader("LLM vs deterministic fallback")
    used = rep.llm.status == "generated"
    (st.success if used else st.info)(
        f"provider `{rep.llm.provider}` · status **{rep.llm.status}** · "
        + (
            "LLM interpretations were screened and kept where supported."
            if used
            else "deterministic template used."
        )
    )
    for n in rep.llm.notes:
        st.caption(n)
    st.subheader("Verification findings")
    ui.verification_banner(rep.verification.status, rep.verification.checks)


def _briefing(run: BriefingRunOut, planned: list[str]) -> None:
    _header("daily_briefing", run.briefing_id, run.status, run.created_at, run.completed_at, run.error)
    st.subheader("Nodes")
    ui.table(node_rows(planned, run.steps))
    st.caption(
        "Briefing steps are persisted when the run completes or fails. Per-node timestamps exist only for "
        "runs recorded after this console was added. Individual tool/HTTP calls are not traced."
    )
    st.markdown(f"**Request parameters:** `{run.params or UNAVAILABLE}`")
    rep = run.report
    if rep is None:
        st.warning("No report persisted: providers and verification unavailable.")
        return
    st.subheader("Data providers and provenance")
    ui.origin_banner(rep.data_origins)
    providers: dict[str, int] = {}
    for s in rep.sources:
        providers[s.get("source_name") or "unknown"] = providers.get(s.get("source_name") or "unknown", 0) + 1
    ui.table([{"source": k, "evidence records": v} for k, v in sorted(providers.items())])
    st.subheader("LLM vs deterministic fallback")
    llm = rep.llm or {}
    (st.success if llm.get("status") == "generated" else st.info)(
        f"provider `{llm.get('provider')}` · status **{llm.get('status')}**"
    )
    for n in llm.get("notes", []):
        st.caption(n)
    st.subheader("Verification findings")
    ui.verification_banner(rep.verification.status, rep.verification.checks)
