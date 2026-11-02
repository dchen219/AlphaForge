"""AlphaForge local agent-testing dashboard (Streamlit).

Run from the repository root (binds to 127.0.0.1 via .streamlit/config.toml):

    uv run python -m streamlit run apps/ui/app.py

Requires the FastAPI app: ``uv run finance-agent serve --offline`` (port 8000) and, for live mode,
``uv run finance-agent serve --port 8001``. See docs/agent_testing_ui.md.
"""

import streamlit as st

from finance_agent.ui import components as ui
from finance_agent.ui.pages import (
    agent_lab,
    company_research,
    daily_briefing,
    dashboard,
    events_explorer,
    historical_replay,
    market_data,
)

st.set_page_config(page_title="AlphaForge Testing Dashboard", page_icon="📈", layout="wide")

pages = {
    "Overview": [
        st.Page(dashboard.render, title="Dashboard", url_path="dashboard", icon="🧭", default=True),
    ],
    "Research": [
        st.Page(company_research.render, title="Company Research", url_path="research", icon="🏢"),
    ],
    "Market Intelligence": [
        st.Page(events_explorer.render, title="Financial Events", url_path="events", icon="🗂️"),
        st.Page(daily_briefing.render, title="Daily Briefing", url_path="briefing", icon="📰"),
    ],
    "Market Data": [
        st.Page(market_data.render, title="Prices, Yields & Calendar", url_path="market", icon="📊"),
    ],
    "Testing": [
        st.Page(agent_lab.render, title="Agent Testing Lab", url_path="lab", icon="🧪"),
        st.Page(historical_replay.render, title="Historical Replay", url_path="replay", icon="⏪"),
    ],
}
nav = st.navigation(pages)

with st.sidebar:
    st.markdown("### AlphaForge")
    st.caption("Local developer & research testing dashboard — not a production app. No trading.")
    for mode in ui.MODES:
        try:
            url = ui.api_url(mode)
        except ValueError as exc:
            st.error(f"{mode}: {exc}")
            continue
        st.caption(f"**{mode}** API: `{url}`")
    with st.expander("Start the backends"):
        st.code(ui.LAUNCH["offline"] + "\n" + ui.LAUNCH["live"], language="bash")
        st.caption(
            "Populate data (CLI only): `uv run finance-agent events ingest --offline` and "
            "`uv run python -m finance_agent market ingest --start 2024-01-02 --end 2026-10-30 --offline`"
        )

nav.run()
