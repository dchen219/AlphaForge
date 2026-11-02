"""Page E — Agent Execution Inspector: stored LangGraph run records (research runs and briefings)."""

from __future__ import annotations

from datetime import datetime
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
                "duration_ms (measured)": s.get("ms") if s and "ms" in s else (UNAVAILABLE if s else "—"),
                "error": s.get("error", "") if s else "",
            }
        )
    return rows


def _duration(start: datetime | None, end: datetime | None) -> str:
    if not start or not end:
        return UNAVAILABLE
    return f"{(end - start).total_seconds():.2f} s (from stored created/completed timestamps)"


def render() -> None:
    st.title("Agent Execution Inspector")
    st.caption(
        "Reads stored run records (`GET /research`, `/research/{id}`, `/intelligence/briefings`). "
        "Only what the workflows recorded is shown; anything not recorded is labelled so."
    )
    mode = ui.mode_selector()
    c, h = ui.backend(mode)
    if c is None:
        return
    ui.backend_caption(h, c)
    kind = st.radio("Workflow", ["company_research", "daily_briefing"], horizontal=True, key="E_kind")
    try:
        planned = c.workflows().get(kind, [])
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
            ids = [r.run_id for r in listing.runs]
        else:
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
            ids = [b.briefing_id for b in blist.briefings]
    except Exception as exc:
        ui.show_error(exc, "Loading run records")
        return
    st.markdown(f"**Planned graph:** {' → '.join(planned)} (any node error → `fail`)")
    st.subheader(f"Stored runs ({len(rows)})")
    ui.table(rows, height=260)
    if not ids:
        st.info("No runs recorded yet. Run company research or generate a briefing first.")
        return
    rid = st.selectbox("Inspect run", ids, key="E_pick")
    try:
        validation.run_id(rid)
        if kind == "company_research":
            _research(c.research_run(rid), planned)
        else:
            _briefing(c.briefing_run(rid), planned)
    except Exception as exc:
        ui.show_error(exc, "Loading run")


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
