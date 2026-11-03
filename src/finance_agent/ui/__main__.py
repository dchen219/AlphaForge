"""``python -m finance_agent.ui`` — start the dashboard bound to 127.0.0.1 (no .exe launcher needed).

Optional environment: ``ALPHAFORGE_UI_PORT`` (default 8501). The bind address is not configurable
here: public deployment is out of scope.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from finance_agent.config.settings import PROJECT_ROOT

APP = PROJECT_ROOT / "apps" / "ui" / "app.py"


def streamlit_argv(port: str | None = None) -> list[str]:
    p = port or os.environ.get("ALPHAFORGE_UI_PORT", "8501")
    if not p.isdigit() or not 1024 <= int(p) <= 65535:
        raise SystemExit(f"ALPHAFORGE_UI_PORT must be a port number 1024-65535, got {p!r}")
    return [
        "streamlit",
        "run",
        str(Path(APP)),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        p,
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
    ]


def main() -> None:
    from streamlit.web import cli

    os.chdir(PROJECT_ROOT)  # picks up .streamlit/config.toml
    sys.argv = streamlit_argv()
    sys.exit(cli.main())


if __name__ == "__main__":
    main()
