"""AlphaForge local agent-testing console (Streamlit).

Run from the repository root (binds to 127.0.0.1 via .streamlit/config.toml):

    uv run python -m streamlit run apps/ui/app.py

Requires the FastAPI app: ``uv run finance-agent serve --offline`` (port 8000) and, for live mode,
``uv run finance-agent serve --port 8001``. See docs/agent_testing_ui.md.
"""

import streamlit as st

from finance_agent.ui import components as ui
from finance_agent.ui.pages import (
    company_research,
    daily_briefing,
    events_explorer,
    execution_inspector,
    historical_replay,
)

st.set_page_config(page_title="AlphaForge Testing Console", page_icon="📈", layout="wide")

pages = [
    st.Page(company_research.render, title="Company Research", url_path="research", icon="🏢", default=True),
    st.Page(events_explorer.render, title="Financial Events", url_path="events", icon="🗂️"),
    st.Page(daily_briefing.render, title="Daily Briefing", url_path="briefing", icon="📰"),
    st.Page(historical_replay.render, title="Historical Replay", url_path="replay", icon="⏪"),
    st.Page(execution_inspector.render, title="Agent Execution", url_path="inspector", icon="🔎"),
]
nav = st.navigation(pages)

with st.sidebar:
    st.markdown("### AlphaForge")
    st.caption("Local developer & research testing console — not a production app. No trading.")
    for mode in ui.MODES:
        try:
            url = ui.api_url(mode)
        except ValueError as exc:
            st.error(f"{mode}: {exc}")
            continue
        st.caption(f"**{mode}** API: `{url}`")
    with st.expander("Start the backends"):
        st.code(ui.LAUNCH["offline"] + "\n" + ui.LAUNCH["live"], language="bash")
        st.caption("Populate events: `uv run finance-agent events ingest --offline`")

nav.run()
