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
    help="AlphaForge v0.1 - agentic financial intelligence and research platform (SEC/FRED data, company research).",
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


# ============================================================== v0.2 event intelligence

events_app = typer.Typer(help="Financial events: ingest, list, inspect, calendar.", no_args_is_help=True)
briefing_app = typer.Typer(help="Daily intelligence briefings.", no_args_is_help=True)
app.add_typer(events_app, name="events")
app.add_typer(briefing_app, name="briefing")


def _date(value: str | None, name: str):
    from datetime import date as _d

    if value is None:
        return None
    try:
        return _d.fromisoformat(value)
    except ValueError:
        _fail(ValueError(f"Invalid {name} {value!r}; use YYYY-MM-DD."))


def _window_start(value: str | None):
    from finance_agent.timeutil import parse_window_start

    try:
        return parse_window_start(value)
    except ValueError:
        _fail(ValueError(f"Invalid --from {value!r}; use YYYY-MM-DD or ISO 8601."))


@events_app.command("ingest")
def events_ingest(
    source: Annotated[
        list[str] | None, typer.Option("--source", "-s", help="sec | fomc | macro | all (repeatable)")
    ] = None,
    as_of: AsOf = None,
    start: Annotated[
        str | None, typer.Option("--start", help="Earliest date (YYYY-MM-DD); default 120 days back.")
    ] = None,
    ticker: Annotated[
        list[str] | None, typer.Option("--ticker", "-t", help="SEC tickers (repeatable)")
    ] = None,
    offline: Offline = False,
) -> None:
    """Ingest events from official sources (manual / scheduler-invoked; no daemon)."""
    from finance_agent.events.service import SOURCES, EventIngestor

    requested = source or ["all"]
    sources = list(SOURCES) if "all" in requested else requested
    bad = [x for x in sources if x not in SOURCES]
    if bad:
        _fail(ValueError(f"Unknown source(s) {bad}; choose from {list(SOURCES)} or 'all'."))
    ctx = _ctx(offline, llm=False)
    report = EventIngestor(ctx).run(
        sources, as_of=_as_of(as_of), start=_date(start, "--start"), tickers=ticker
    )
    for name, stats in report.per_source.items():
        if "error" in stats:
            console.print(f"[red]{name}: FAILED[/] {redact(stats['error'])}")
        else:
            console.print(
                f"{name}: inserted={stats['inserted']} updated={stats['updated']} unchanged={stats['unchanged']} "
                f"sources_added={stats['sources_added']} conflicts={len(stats['conflicts'])}"
            )
    if not report.ok:
        raise typer.Exit(code=1)


def _events_table(evs, title: str) -> None:
    from finance_agent.timeutil import ET

    t = Table(
        "Available/Scheduled (ET)",
        "Type",
        "Subtype",
        "Status",
        "Importance",
        "Title",
        "Event ID",
        title=title,
    )
    for e in evs:
        when = e.available_at or e.scheduled_for
        score = f"{e.importance_level or '-'} {e.importance_score if e.importance_score is not None else ''}"
        t.add_row(
            when.astimezone(ET).strftime("%Y-%m-%d %H:%M") if when else "-",
            str(e.event_type),
            str(e.event_subtype),
            str(e.event_status),
            score,
            e.title[:70],
            e.event_id,
        )
    console.print(t)


@events_app.command("list")
def events_list(
    start: Annotated[
        str | None, typer.Option("--from", help="Window start (available_at), YYYY-MM-DD or ISO")
    ] = None,
    end: Annotated[
        str | None, typer.Option("--to", help="Window end (available_at), YYYY-MM-DD or ISO")
    ] = None,
    as_of: AsOf = None,
    category: Annotated[list[str] | None, typer.Option("--category", "-c")] = None,
    ticker: Annotated[str | None, typer.Option("--ticker")] = None,
    level: Annotated[
        list[str] | None, typer.Option("--level", help="HIGH | MEDIUM | LOW | INSUFFICIENT_DATA")
    ] = None,
    limit: Annotated[int, typer.Option(min=1, max=1000)] = 100,
    offline: Offline = False,
    output: Annotated[str, typer.Option("--format", help="table | json")] = "table",
) -> None:
    """List events that became available in a window (optionally as of a historical cutoff)."""
    from finance_agent.events.store import EventStore

    ctx = _ctx(offline, llm=False)
    evs = EventStore(ctx.db).query(
        as_of=_as_of(as_of),
        start=_window_start(start),
        end=_as_of(end),
        categories=[c.upper() for c in category] if category else None,
        ticker=ticker,
        levels=[x.upper() for x in level] if level else None,
        limit=limit,
    )
    if output == "json":
        sys.stdout.write(json.dumps([e.model_dump(mode="json") for e in evs], indent=2) + "\n")
        return
    _events_table(evs, f"{len(evs)} events")


@events_app.command("show")
def events_show(event_id: str, offline: Offline = False) -> None:
    """Show one event with its sources, comparisons and importance components (JSON)."""
    from finance_agent.events.store import EventStore

    store = EventStore(_ctx(offline, llm=False).db)
    ev = store.get(event_id)
    if ev is None:
        _fail(FinanceAgentError(f"Event {event_id} not found"))
    out = ev.model_dump(mode="json")
    out["sources"] = [
        {
            "source_name": r.source_name,
            "role": r.role,
            "url": r.source_url,
            "snapshot_sha256": r.snapshot_sha256,
            "excerpt": r.excerpt,
        }
        for r in store.sources(event_id)
    ]
    sys.stdout.write(json.dumps(out, indent=2) + "\n")


@events_app.command("calendar")
def events_calendar(
    start: Annotated[str | None, typer.Option("--from")] = None,
    end: Annotated[str | None, typer.Option("--to")] = None,
    offline: Offline = False,
) -> None:
    """Scheduled releases/meetings (calendar only - not evidence that anything occurred)."""
    from datetime import timedelta

    from finance_agent.events.store import EventStore
    from finance_agent.timeutil import utcnow

    s = _window_start(start) or utcnow()
    e = _as_of(end) or s + timedelta(days=30)
    _events_table(
        EventStore(_ctx(offline, llm=False).db).calendar(start=s, end=e), "Scheduled (not occurred)"
    )


@briefing_app.command("daily")
def briefing_daily(
    date: Annotated[str | None, typer.Option("--date", help="New York date; cutoff 23:59:59 ET")] = None,
    as_of: AsOf = None,
    window_hours: Annotated[int | None, typer.Option("--window-hours", min=1, max=744)] = None,
    no_llm: Annotated[bool, typer.Option("--no-llm", help="Deterministic template narrative only.")] = False,
    offline: Offline = False,
    ingest: Annotated[
        bool, typer.Option("--ingest/--no-ingest", help="Ingest sources for the window first.")
    ] = True,
    output: Annotated[str, typer.Option("--format", help="markdown | json")] = "markdown",
    save: Annotated[Path | None, typer.Option(help="Also write the briefing to this file.")] = None,
) -> None:
    """Generate the daily intelligence briefing (fully functional without an LLM)."""
    from datetime import timedelta

    from finance_agent.events.briefing import BriefingRequest
    from finance_agent.events.service import EventIngestor
    from finance_agent.workflows.briefing import BriefingWorkflow, resolve_window

    if date:
        _date(date, "--date")
    try:
        req = BriefingRequest(date=date, as_of=_as_of(as_of), window_hours=window_hours, use_llm=not no_llm)
    except ValueError as exc:
        _fail(exc)
    ctx = _ctx(offline, llm=not no_llm)
    if ingest:
        cutoff, start = resolve_window(req)
        rep = EventIngestor(ctx).run(as_of=cutoff, start=(start - timedelta(days=45)).date())
        for e in rep.errors:
            err.print(f"[yellow]ingestion warning:[/] {redact(e)}")
    state = BriefingWorkflow(ctx).run(req)
    if state.get("error"):
        err.print(f"[bold red]Briefing failed:[/] {redact(state['error'])}")
        raise typer.Exit(code=1)
    r = state["report"]
    text = r.model_dump_json(indent=2) if output == "json" else r.markdown
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    if save:
        save.write_text(text, encoding="utf-8")
    err.print(
        f"[dim]briefing_id={r.briefing_id} verification={r.verification.status} llm={r.llm['status']}[/]"
    )


@briefing_app.command("show")
def briefing_show(briefing_id: str, output: Annotated[str, typer.Option("--format")] = "markdown") -> None:
    """Show a stored briefing."""
    from finance_agent.events.store import BriefingStore

    rec = BriefingStore(_ctx(False, llm=False).db).get(briefing_id)
    if rec is None:
        _fail(FinanceAgentError(f"Briefing {briefing_id} not found"))
    if output == "json":
        text = json.dumps(rec.report_json, indent=2)
    else:
        text = rec.markdown or f"status={rec.status} error={rec.error}"
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))


def main() -> None:  # pragma: no cover
    app()
