"""API application entry point.

Run with:  uv run uvicorn apps.api.main:app --reload
      or:  uv run finance-agent serve
The application factory lives in ``finance_agent.interfaces.api`` so it is
importable and testable from the installed package.
"""

from finance_agent.interfaces.api import create_app

app = create_app()
