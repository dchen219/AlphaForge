"""Command-line interface: ``uv run finance-agent --help``."""

from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from finance_agent.config.logging import configure_logging, redact
from finance_agent.config.settings import get_settings
from finance_agent.errors import FinanceAgentError
from finance_agent.research.formatting import fmt_value
from finance_agent.research.report import origin_banner
from finance_agent.timeutil import parse_as_of

app = typer.Typer(
    help="Finance Agent v0.1 - SEC/FRED data infrastructure and company research.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console(width=None if sys.stdout.isatty() else 200)
err = Console(stderr=True)

AsOf = Annotated[str | None, typer.Option("--as-of", help="Point-in-time cutoff, YYYY-MM-DD or ISO 8601.")]
Offline = Annotated[bool, typer.Option("--offline", help="Use saved fixtures instead of the network.")]


def _ctx(offline: bool, llm: bool = True):
    settings = get_settings()
    if offline:
        settings = settings.model_copy(update={"offline_mode": True})
    if not llm:
        settings = settings.model_copy(update={"llm_enabled": False})
    configure_logging(settings.log_level if settings.log_level != "INFO" else "WARNING", json_output=False)
    from finance_agent.services import AppContext

    return AppContext(settings)


def _fail(exc: Exception) -> None:
    err.print(f"[bold red]Error:[/] {redact(str(exc))}")
    raise typer.Exit(code=1)


def _as_of(value: str | None):
    try:
        return parse_as_of(value)
    except ValueError:
        _fail(ValueError(f"Invalid --as-of {value!r}; use YYYY-MM-DD or ISO 8601."))


@app.command("init-db")
def init_db() -> None:
    """Create/upgrade the local database (Alembic migrations)."""
    from finance_agent.storage.db import upgrade_database

    s = get_settings()
    upgrade_database(s.database_url)
    console.print(f"Database ready: {s.database_url}")


@app.command()
def company(ticker: str, offline: Offline = False) -> None:
    """Show SEC company profile and recent 10-K/10-Q filings."""
    try:
        ctx = _ctx(offline)
        ov = ctx.company(ticker)
    except FinanceAgentError as exc:
        _fail(exc)
    p = ov.profile
    console.print(
        f"[bold]{p.name}[/] ({ov.ticker}) CIK {p.cik} - FYE {p.fiscal_year_end} - {p.sic_description}"
    )
    console.print(f"[dim]{origin_banner(ov.data_origin)}[/]")
    t = Table("Form", "Filed", "Period", "Accession")
    for f in ov.recent_filings[:12]:
        t.add_row(f.form, str(f.filing_date), str(f.report_date or ""), f.accession)
    console.print(t)


@app.command()
def financials(
    ticker: str,
    quarters: Annotated[int, typer.Option(min=1, max=20, help="Number of recent quarters.")] = 8,
    as_of: AsOf = None,
    offline: Offline = False,
    output: Annotated[str, typer.Option("--format", help="table | json")] = "table",
) -> None:
    """Deterministic data inspection: normalized quarterly financials and ratios (no LLM)."""
    try:
        ctx = _ctx(offline, llm=False)
        res = ctx.financials(ticker, quarters, _as_of(as_of))
    except FinanceAgentError as exc:
        _fail(exc)
    if output == "json":
        sys.stdout.write(res.model_dump_json(indent=2) + "\n")
        return
    fin = res.financials
    focus = fin.focus()
    labels = [p.label for p in focus]
    console.print(
        f"[bold]{fin.company_name}[/] ({fin.ticker}) CIK {fin.cik} - as of "
        f"{fin.as_of.isoformat() if fin.as_of else 'latest'}"
    )
    console.print(f"[dim]{origin_banner(fin.data_origin)}[/]")
    t = Table("Metric", *labels, title="Normalized values (USD; * = derived from cumulative YTD/annual)")
    off = len(fin.quarters) - len(focus)
    for m in (
        "revenue",
        "gross_profit",
        "operating_income",
        "net_income",
        "operating_cash_flow",
        "capex",
        "cash",
        "total_debt",
    ):
        row = fin.series.get(m, [])[off:]
        cells = [
            "-"
            if v is None
            else fmt_value(v.value, v.unit) + ("*" if v.method == "derived_ytd_difference" else "")
            for v in row
        ]
        t.add_row(m, *cells)
    console.print(t)
    calcs = {(c.name, c.period_label): c for c in res.calculations}
    t2 = Table("Ratio", *labels, title="Deterministic calculations")
    for name in (
        "revenue_yoy",
        "revenue_qoq",
        "gross_margin",
        "operating_margin",
        "net_margin",
        "free_cash_flow",
        "fcf_margin",
        "capex_to_revenue",
        "capex_yoy",
        "ocf_to_capex",
        "net_debt",
    ):
        t2.add_row(
            name,
            *[
                fmt_value(calcs[(name, lb)].value, calcs[(name, lb)].unit) if (name, lb) in calcs else "-"
                for lb in labels
            ],
        )
    console.print(t2)
    for w in fin.warnings:
        console.print(f"[yellow]warning:[/] {w}")
    if fin.missing:
        console.print(
            f"[yellow]{len(fin.missing)} metric/period values unavailable[/] "
            f"(e.g. {fin.missing[0].metric} {fin.missing[0].period_label}: {fin.missing[0].reason})"
        )


@app.command()
def macro(
    series_id: str,
    as_of: AsOf = None,
    start: Annotated[str | None, typer.Option(help="Observation start YYYY-MM-DD")] = None,
    end: Annotated[str | None, typer.Option(help="Observation end YYYY-MM-DD")] = None,
    offline: Offline = False,
    output: Annotated[str, typer.Option("--format", help="table | json")] = "table",
) -> None:
    """FRED/ALFRED series observations with vintage metadata."""
    try:
        ctx = _ctx(offline, llm=False)
        res = ctx.macro(
            series_id,
            as_of=_as_of(as_of),
            start=date.fromisoformat(start) if start else None,
            end=date.fromisoformat(end) if end else None,
        )
    except (FinanceAgentError, ValueError) as exc:
        _fail(exc)
    if output == "json":
        sys.stdout.write(res.model_dump_json(indent=2) + "\n")
        return
    console.print(f"[bold]{res.series.series_id}[/] {res.series.title} ({res.series.units})")
    console.print(
        f"[dim]origin: {res.data_origin}; vintage date: {res.effective_vintage_date or 'latest'}[/]"
    )
    t = Table("Observation", "Value", "Vintage start", "Vintage end", "Available at (UTC)")
    for o in res.observations[-24:]:
        t.add_row(
            str(o.observation_date),
            "missing" if o.missing else f"{o.value:g}",
            str(o.realtime_start),
            str(o.realtime_end),
            o.available_at.isoformat(),
        )
    console.print(t)
    for w in res.warnings:
        console.print(f"[yellow]note:[/] {w}")


@app.command()
def research(
    ticker: Annotated[
        str | None, typer.Argument(help="Ticker (optional if --question names a company)")
    ] = None,
    quarters: Annotated[int, typer.Option(min=2, max=20)] = 8,
    question: Annotated[str | None, typer.Option("--question", "-q")] = None,
    as_of: AsOf = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Template summary only; skip LLM.")] = False,
    offline: Offline = False,
    output: Annotated[str, typer.Option("--format", help="markdown | json")] = "markdown",
    save: Annotated[Path | None, typer.Option(help="Also write the report to this file.")] = None,
) -> None:
    """Run the company research workflow and print the report."""
    from finance_agent.schemas.research import ResearchRequest
    from finance_agent.workflows.company_research import CompanyResearchWorkflow

    try:
        req = ResearchRequest(
            ticker=ticker, question=question, quarters=quarters, as_of=_as_of(as_of), use_llm=not no_llm
        )
    except ValueError as exc:
        _fail(exc)
    ctx = _ctx(offline, llm=not no_llm)
    state = CompanyResearchWorkflow(ctx).run(req)
    if state.get("error"):
        err.print(f"[bold red]Research run {state['run_id']} failed:[/] {redact(state['error'])}")
        raise typer.Exit(code=1)
    rep = state["report"]
    text = rep.model_dump_json(indent=2) if output == "json" else rep.markdown
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    if save:
        save.write_text(text, encoding="utf-8")
    err.print(f"[dim]run_id={rep.run_id} verification={rep.verification.status} llm={rep.llm.status}[/]")


@app.command()
def show(run_id: str, output: Annotated[str, typer.Option("--format")] = "markdown") -> None:
    """Show a stored research run."""
    from finance_agent.services import AppContext

    ctx = AppContext(get_settings())
    found = ctx.repo.get_run(run_id)
    if not found:
        _fail(FinanceAgentError(f"Run {run_id} not found"))
    run, rep = found
    if rep is None:
        console.print(json.dumps({"status": run.status, "error": run.error, "steps": run.steps}, indent=2))
        return
    text = json.dumps(rep.report_json, indent=2) if output == "json" else rep.markdown
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, offline: Offline = False) -> None:
    """Launch the HTTP API (uvicorn)."""
    import os

    import uvicorn

    if offline:
        os.environ["OFFLINE_MODE"] = "true"
        get_settings.cache_clear()
    uvicorn.run("finance_agent.interfaces.asgi:app", host=host, port=port)


def main() -> None:  # pragma: no cover
    app()
