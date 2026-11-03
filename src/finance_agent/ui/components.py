"""Shared Streamlit widgets for the testing console."""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, time
from typing import Any

import streamlit as st

from finance_agent.interfaces.api import HealthResponse
from finance_agent.timeutil import ET
from finance_agent.ui import validation
from finance_agent.ui.client import AlphaForgeClient, ApiError
from finance_agent.ui.labels import origin_label, verification_kind

MODES = {
    "offline": "Offline — recorded fixtures (real SEC/Fed/BLS/BEA/ALFRED snapshots; FRED and prices synthetic)",
    "live": "Live — official sources over the network",
}
DEFAULT_URLS = {"offline": "http://127.0.0.1:8000", "live": "http://127.0.0.1:8001"}
ENV_URLS = {"offline": "ALPHAFORGE_API_URL", "live": "ALPHAFORGE_LIVE_API_URL"}
LAUNCH = {
    "offline": "uv run finance-agent serve --offline --port 8000",
    "live": 'SEC_USER_AGENT="Name email" uv run finance-agent serve --port 8001',
}


def api_url(mode: str) -> str:
    """Backend URL for a mode, from the environment only (never typed into the browser)."""
    return validation.api_base_url(os.environ.get(ENV_URLS[mode], DEFAULT_URLS[mode]))


def client(mode: str) -> AlphaForgeClient:
    key = f"_client_{mode}"
    if key not in st.session_state:
        st.session_state[key] = AlphaForgeClient(api_url(mode))
    return st.session_state[key]


def mode_selector(label: str = "Data mode") -> str:
    """Offline/live selector whose value persists across pages (widget state does not)."""
    current = st.session_state.get("_data_mode", "offline")
    choice = st.radio(
        label,
        list(MODES),
        index=list(MODES).index(current),
        format_func=lambda m: MODES[m],
        horizontal=True,
        key=f"_mode_widget_{label}",
    )
    st.session_state["_data_mode"] = choice
    return choice


def backend(mode: str) -> tuple[AlphaForgeClient | None, HealthResponse | None]:
    """Client + health for ``mode``; renders an error and returns (None, None) if unusable."""
    try:
        c = client(mode)
        h = c.health()
    except validation.ValidationError as exc:
        st.error(f"Backend URL rejected: {exc}")
        return None, None
    except ApiError as exc:
        st.error(f"{mode.title()} API unavailable at {api_url(mode)}: {exc.message}")
        st.code(LAUNCH[mode], language="bash")
        return None, None
    if h.offline_mode != (mode == "offline"):
        st.error(
            f"The API at {c.base_url} reports offline_mode={h.offline_mode}, which does not match the "
            f"selected {mode} mode. Refusing to send requests so data are not mislabelled."
        )
        return None, None
    return c, h


def backend_caption(h: HealthResponse, c: AlphaForgeClient) -> None:
    creds = ", ".join(f"{k}={'set' if v else 'not set'}" for k, v in h.credentials_configured.items())
    st.caption(
        f"API {c.base_url} · v{h.version} · database {h.database} · LLM provider `{h.llm_provider}` · "
        f"credentials: {creds}"
    )


def show_error(exc: Exception, action: str) -> None:
    if isinstance(exc, ApiError):
        hint = {
            "unreachable": "Start the API (see sidebar).",
            "timeout": "The request exceeded the client timeout; the server may still be working.",
            "contract": "The API response did not match the expected schema.",
        }.get(exc.kind, "")
        st.error(f"{action} failed — HTTP {exc.status or '—'} ({exc.kind}): {exc.message} {hint}".strip())
    elif isinstance(exc, validation.ValidationError):
        st.warning(f"Invalid input: {exc}")
    else:
        st.error(f"{action} failed: {type(exc).__name__}: {exc}")


def origin_banner(origins: str | list[str] | None) -> None:
    lab = origin_label(origins)
    raw = origins if isinstance(origins, str) else ", ".join(origins or []) or "none reported"
    getattr(st, lab.kind)(f"**Data origin:** {lab.text}  \n`{raw}`")


def verification_banner(status: str | None, checks: list[Any]) -> None:
    getattr(st, verification_kind(status))(f"**Verification:** {status or 'not available'}")
    with st.expander(f"Verification checks ({len(checks)})", expanded=status == "failed"):
        for ch in checks:
            icon = {"passed": "✅", "warning": "⚠️", "failed": "❌"}.get(ch.status, "•")
            st.markdown(f"{icon} **{ch.name}** — {ch.status}")
            for d in ch.details:
                st.markdown(f"  - {d}")


def cutoff_input(label: str, key: str, default: date | None = None, help: str | None = None) -> str | None:
    """Optional date + optional New York time → API ``as_of`` string."""
    cols = st.columns([2, 1, 2])
    use = cols[0].checkbox(f"{label}", value=default is not None, key=f"{key}_on", help=help)
    if not use:
        return None
    d = cols[0].date_input(
        "Date", value=default or date.today(), key=f"{key}_date", label_visibility="collapsed"
    )
    with_time = cols[1].checkbox(
        "Time", key=f"{key}_t_on", help="Unchecked = 23:59:59 New York (end of day)."
    )
    t = cols[2].time_input("Time (New York)", value=time(16, 0), key=f"{key}_time") if with_time else None
    return validation.cutoff(d, t)


def fixture_cutoff_day(h: HealthResponse) -> date | None:
    """New York date of the offline fixture recording cutoff (None when the API is live)."""
    if not h.fixture_event_window:
        return None
    return datetime.fromisoformat(h.fixture_event_window["as_of"]).astimezone(ET).date()


def parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def fmt_dt(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return f"{dt.astimezone(ET):%Y-%m-%d %H:%M:%S %Z}"


def json_expander(title: str, obj: Any) -> None:
    with st.expander(title):
        data = obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj
        st.code(json.dumps(data, indent=2, default=str)[:200_000], language="json")


def export_buttons(
    stem: str, markdown: str | None, obj: Any, origins: str | list[str] | None, key: str
) -> None:
    """Download the API's own report text and run JSON. The export is prefixed with the origin label
    so a saved synthetic report cannot be mistaken for real findings."""
    lab = origin_label(origins)
    raw = origins if isinstance(origins, str) else ", ".join(origins or []) or "none reported"
    cols = st.columns(2)
    if markdown:
        text = f"> **Data origin (dashboard export): {lab.text}** — `{raw}`\n\n{markdown}"
        cols[0].download_button(
            "Download report (Markdown)", text, file_name=f"{stem}.md", mime="text/markdown", key=f"{key}_md"
        )
    else:
        cols[0].caption("No Markdown report produced by the API for this run.")
    data = obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj
    cols[1].download_button(
        "Download run (JSON)",
        json.dumps({"data_origin_label": lab.text, "run": data}, indent=2, default=str),
        file_name=f"{stem}.json",
        mime="application/json",
        key=f"{key}_json",
    )


def display_rows(rows: list[dict]) -> list[dict]:
    """Columns mixing text with numbers/booleans (e.g. a rank or '—') are shown as text so the table
    renders reliably; values themselves are not changed or rounded."""
    cols = {k for r in rows for k in r}
    mixed = set()
    for k in cols:
        kinds = {type(r.get(k)) for r in rows if r.get(k) is not None}
        if (str in kinds and len(kinds) > 1) or (dict in kinds or list in kinds):
            mixed.add(k)
    if not mixed:
        return rows
    return [{k: (str(v) if k in mixed and v is not None else v) for k, v in r.items()} for r in rows]


def table(rows: list[dict], link_columns: tuple[str, ...] = (), height: int | None = None) -> None:
    if not rows:
        st.info("No rows.")
        return
    config = {c: st.column_config.LinkColumn(c) for c in link_columns}
    kwargs = {"height": height} if height else {}
    st.dataframe(display_rows(rows), hide_index=True, column_config=config, width="stretch", **kwargs)
