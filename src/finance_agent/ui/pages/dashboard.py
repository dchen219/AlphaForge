"""Dashboard — backend health, data sources, stored runs and ingestion state (read-only)."""

from __future__ import annotations

import streamlit as st

from finance_agent.ui import components as ui
from finance_agent.ui.client import ApiError
from finance_agent.ui.labels import LIVE, REAL_SNAPSHOT, SYNTHETIC, UNKNOWN, origin_label


def _health_rows() -> list[dict]:
    """Health of both configured backends; an unreachable one is reported, not hidden."""
    rows = []
    for mode in ui.MODES:
        try:
            url = ui.api_url(mode)
            h = ui.client(mode).health()
        except ApiError as exc:
            rows.append({"backend": mode, "url": url, "reachable": "no", "detail": exc.message[:120]})
            continue
        except ValueError as exc:
            rows.append({"backend": mode, "url": "rejected", "reachable": "no", "detail": str(exc)})
            continue
        match = h.offline_mode == (mode == "offline")
        rows.append(
            {
                "backend": mode,
                "url": url,
                "reachable": "yes",
                "status": h.status,
                "version": h.version,
                "database": h.database,
                "offline_mode": h.offline_mode,
                "mode matches": "yes" if match else "NO — requests refused",
                "llm_provider": h.llm_provider,
            }
        )
    return rows


def render() -> None:
    st.title("Dashboard")
    st.caption(
        "Read-only overview: `GET /health`, `/system/overview`, `/research`, `/intelligence/briefings`. "
        "Credential values are never sent to the browser — only whether each one is configured."
    )
    st.subheader("Backend health")
    ui.table(_health_rows())

    mode = ui.mode_selector()
    c, h = ui.backend(mode)
    if c is None:
        return
    ui.backend_caption(h, c)
    if h.fixture_event_window:
        st.info(
            f"Offline fixture window: events recorded from {h.fixture_event_window['start']} to "
            f"{ui.fmt_dt(ui.parse_dt(h.fixture_event_window['as_of']))}. Historical queries outside it "
            "return nothing — data are reported missing, never filled."
        )

    with st.expander("Data-origin labels used on every page", expanded=False):
        for lab in (LIVE, REAL_SNAPSHOT, SYNTHETIC, UNKNOWN):
            getattr(st, lab.kind)(lab.text)

    try:
        ov = c.overview()
    except Exception as exc:
        ui.show_error(exc, "Loading system overview")
        return

    st.subheader("Data sources and credentials")
    st.caption(
        f"Mode: **{'offline' if ov.offline_mode else 'live'}**. Offline mode serves the recorded origin "
        "shown; live mode needs the credentials listed. HTTP ingestion is "
        f"**{'enabled (admin token configured)' if ov.http_ingestion_enabled else 'disabled'}**; "
        "this dashboard never ingests."
    )
    ui.table(
        [
            {
                "source": s.name,
                "used for": s.used_for,
                "credentials": ", ".join(
                    f"{x.env_var}: {'configured' if x.configured else 'MISSING'}" for x in s.credentials
                )
                or "none required",
                "live ready": "yes" if s.live_ready else "no",
                "offline serves": s.offline_origin,
                "offline label": origin_label(s.offline_origin).text
                if s.offline_origin.startswith("fixture:")
                else "—",
                "note": s.live_note,
            }
            for s in ov.sources
        ]
    )
    missing = [x.env_var for s in ov.sources for x in s.credentials if not x.configured]
    if missing:
        st.warning(
            "Not configured: " + ", ".join(sorted(set(missing))) + ". Offline fixtures and the deterministic "
            "fallback keep every page usable without them."
        )

    st.subheader("Latest ingestion state")
    left, right = st.columns(2)
    with left:
        st.markdown("**Event store** (contents per source)")
        st.caption(
            "Event ingestion reports are printed by the CLI but not persisted, so per-run results are "
            "unavailable; this is what the database contains."
        )
        ui.table(
            [
                {
                    "source": e.source_name,
                    "events": e.events,
                    "origins": ", ".join(e.data_origins),
                    "first available (NY)": ui.fmt_dt(e.first_available_at),
                    "last available (NY)": ui.fmt_dt(e.last_available_at),
                    "last ingested (NY)": ui.fmt_dt(e.last_ingested_at),
                }
                for e in ov.event_store
            ]
        )
        if not ov.event_store:
            st.code("uv run finance-agent events ingest --offline", language="bash")
    with right:
        st.markdown(f"**Market data** — {ov.market_instruments} instruments; latest ingestion runs")
        ui.table(
            [
                {
                    "started (NY)": ui.fmt_dt(r.started_at),
                    "provider": r.provider,
                    "status": r.status,
                    "origin": r.data_origin,
                    "label": origin_label(r.data_origin).text if r.data_origin else "—",
                    "range": f"{r.params.get('start')} → {r.params.get('end')}",
                    "versions inserted": r.stats.get("versions_inserted"),
                    "errors": len(r.errors),
                }
                for r in ov.market_runs
            ]
        )
        if any("synthetic" in (r.data_origin or "") for r in ov.market_runs):
            st.error("Stored price bars include SYNTHETIC fixtures — never live market prices.")
        if not ov.market_runs:
            st.code(
                "uv run python -m finance_agent market ingest --start 2024-01-02 --end 2026-10-30 --offline",
                language="bash",
            )
        if ov.market_quality_counts:
            st.caption(
                "Quality flags: " + ", ".join(f"{k}={v}" for k, v in sorted(ov.market_quality_counts.items()))
            )

    st.subheader("Recent workflow runs")
    left, right = st.columns(2)
    try:
        runs = c.research_runs(limit=10)
        briefs = c.briefings(limit=10)
    except Exception as exc:
        ui.show_error(exc, "Loading recent runs")
        return
    with left:
        st.markdown(
            "**Company research** — "
            + (", ".join(f"{k}: {v}" for k, v in sorted(ov.research_runs.items())) or "no runs")
        )
        ui.table(
            [
                {
                    "created (NY)": ui.fmt_dt(r.created_at),
                    "ticker": r.ticker,
                    "status": r.status,
                    "verification": r.verification_status or "not available",
                    "as_of": ui.fmt_dt(r.as_of) if r.as_of else "latest",
                }
                for r in runs.runs
            ]
        )
    with right:
        st.markdown(
            "**Daily briefings** — "
            + (", ".join(f"{k}: {v}" for k, v in sorted(ov.briefings.items())) or "no runs")
        )
        ui.table(
            [
                {
                    "created (NY)": ui.fmt_dt(b.created_at),
                    "cutoff (NY)": ui.fmt_dt(b.as_of),
                    "status": b.status,
                    "verification": b.verification_status or "not available",
                    "origin": origin_label(b.data_origins).text,
                }
                for b in briefs.briefings
            ]
        )
