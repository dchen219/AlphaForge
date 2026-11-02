"""ASGI entry point: ``uvicorn finance_agent.interfaces.asgi:app``."""

from finance_agent.interfaces.api import create_app

app = create_app()
