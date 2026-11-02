"""v0.3-A market-data HTTP routes (mounted by ``interfaces.api.create_app``).

All reads are local-database queries. Ingestion (network, provider quota) is administrative:
disabled unless INGEST_ADMIN_TOKEN is set, and then only from the local machine with a
matching X-Admin-Token header.
"""

from __future__ import annotations

import hmac
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from pydantic import BaseModel, Field

from finance_agent.errors import DataUnavailableError, NotFoundError
from finance_agent.market.calendar import get_calendar
from finance_agent.market.ingest import MarketDataIngestor
from finance_agent.market.schemas import AdjustmentBasis
from finance_agent.market.service import MODES, MarketDataService
from finance_agent.market.store import MarketStore
from finance_agent.market.universe import YIELD_SERIES
from finance_agent.market.yields import YieldIngestor, yield_series
from finance_agent.timeutil import parse_as_of

router = APIRouter(prefix="/market", tags=["market"])
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
Ticker = Annotated[str, Path(pattern=r"^[A-Za-z0-9.\-]{1,16}$")]
BASES = {
    "raw": AdjustmentBasis.RAW,
    "split_adjusted": AdjustmentBasis.SPLIT_ADJUSTED,
    "total_return": AdjustmentBasis.TOTAL_RETURN,
}


def get_ctx(request: Request):
    return request.app.state.ctx


Ctx = Annotated[Any, Depends(get_ctx)]


def _date(v: str | None, name: str, default: date | None = None) -> date:
    if v is None:
        if default is None:
            raise HTTPException(422, f"{name} is required (YYYY-MM-DD)")
        return default
    try:
        return date.fromisoformat(v)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid {name}: {v!r} (YYYY-MM-DD)") from exc


def _as_of(v: str | None) -> datetime | None:
    try:
        return parse_as_of(v)
    except ValueError as exc:
        raise HTTPException(422, f"Invalid as_of: {v!r}") from exc


def _basis(v: str) -> AdjustmentBasis:
    if v not in BASES:
        raise HTTPException(422, f"basis must be one of {list(BASES)} (no default substitution)")
    return BASES[v]


def _mode(v: str) -> str:
    if v not in MODES:
        raise HTTPException(422, f"mode must be one of {list(MODES)}")
    return v


def _run(fn):
    try:
        return fn()
    except NotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except (DataUnavailableError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/instruments")
def instruments(ctx: Ctx) -> dict:
    store = MarketStore(ctx.db)
    items = store.instruments()
    return {"count": len(items), "instruments": items, "yield_series": YIELD_SERIES}


@router.get("/prices/{ticker}")
def prices(
    ticker: Ticker,
    ctx: Ctx,
    start: Annotated[str, Query()],
    end: Annotated[str, Query()],
    basis: Annotated[str, Query(description="raw | split_adjusted | total_return")] = "raw",
    mode: Annotated[str, Query(description="point_in_time | research")] = "point_in_time",
    as_of: Annotated[str | None, Query(description="cutoff; default now (point_in_time)")] = None,
    include_provisional: bool = False,
) -> dict:
    svc = MarketDataService(ctx.db)
    ps = _run(
        lambda: svc.prices(
            ticker,
            _date(start, "start"),
            _date(end, "end"),
            basis=_basis(basis),
            mode=_mode(mode),
            as_of=_as_of(as_of),
            include_provisional=include_provisional,
        )
    )
    return {
        "instrument_id": ps.instrument_id,
        "contract": ps.contract(),
        "factors": [f.model_dump(mode="json") for f in ps.factors.factors],
        "rows": ps.rows,
        "missing_sessions": {str(d): k for d, k in ps.gaps.items()},
        "data_origins": ps.data_origins,
        "notes": ps.notes,
    }


@router.get("/returns/{ticker}")
def returns(
    ticker: Ticker,
    ctx: Ctx,
    start: Annotated[str, Query()],
    end: Annotated[str, Query()],
    basis: Annotated[str, Query(description="required: raw | split_adjusted | total_return")],
    kind: Annotated[str, Query(pattern="^(simple|log)$")] = "simple",
    mode: str = "point_in_time",
    as_of: str | None = None,
    window: Annotated[int, Query(ge=2, le=252)] = 20,
) -> dict:
    svc = MarketDataService(ctx.db)
    return _run(
        lambda: svc.returns(
            ticker,
            _date(start, "start"),
            _date(end, "end"),
            basis=_basis(basis),
            kind=kind,
            mode=_mode(mode),
            as_of=_as_of(as_of),
            vol_window=window,
        )
    )


@router.get("/correlations")
def correlations(
    ctx: Ctx,
    tickers: Annotated[str, Query(description="comma-separated, 2-10 tickers")],
    start: Annotated[str, Query()],
    end: Annotated[str, Query()],
    basis: Annotated[str, Query()],
    mode: str = "point_in_time",
    as_of: str | None = None,
) -> dict:
    names = [t.strip().upper() for t in tickers.split(",") if t.strip()]
    if not 2 <= len(names) <= 10:
        raise HTTPException(422, "provide 2-10 tickers")
    svc = MarketDataService(ctx.db)
    return _run(
        lambda: svc.correlations(
            names,
            _date(start, "start"),
            _date(end, "end"),
            basis=_basis(basis),
            mode=_mode(mode),
            as_of=_as_of(as_of),
        )
    )


@router.get("/calendar")
def calendar(
    date_: Annotated[str | None, Query(alias="date")] = None,
    start: Annotated[str | None, Query(alias="from")] = None,
    end: Annotated[str | None, Query(alias="to")] = None,
    event_time: Annotated[str | None, Query(description="ISO timestamp with offset to align")] = None,
) -> dict:
    cal = get_calendar()
    out: dict[str, Any] = {"calendar": cal.name, "source": "exchange_calendars (XNYS)"}

    def run():
        if date_:
            d = _date(date_, "date")
            out["date"] = d
            out["is_session"] = cal.is_session(d)
            out["session"] = cal.session(d) if out["is_session"] else None
            out["previous_session"] = cal.previous_session(d)
            out["next_session"] = cal.next_session(d)
        if start or end:
            a, b = _date(start, "from"), _date(end, "to")
            if (b - a).days > 400:
                raise ValueError("range limited to 400 days")
            out["sessions"] = cal.session_table(a, b)
        if event_time:
            try:
                t = datetime.fromisoformat(event_time)
            except ValueError as exc:
                raise ValueError(f"invalid event_time {event_time!r}") from exc
            if t.tzinfo is None:
                raise ValueError("event_time must include a UTC offset")
            out["alignment"] = cal.align(t).__dict__
        return out

    return _run(run)


@router.get("/yields/{series_id}")
def yields(
    series_id: Annotated[str, Path(pattern=r"^[A-Za-z0-9]{1,16}$")],
    ctx: Ctx,
    start: Annotated[str, Query()],
    end: Annotated[str, Query()],
    as_of: str | None = None,
) -> dict:
    sid = series_id.upper()
    if sid not in YIELD_SERIES:
        raise HTTPException(404, f"unknown yield series {sid}; supported: {list(YIELD_SERIES)}")
    return yield_series(MarketStore(ctx.db), sid, _date(start, "start"), _date(end, "end"), _as_of(as_of))


@router.get("/quality")
def quality(
    ctx: Ctx,
    ticker: str | None = None,
    check: str | None = None,
    severity: Annotated[str | None, Query(pattern="^(INFO|WARNING|ERROR)$")] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> dict:
    store = MarketStore(ctx.db)
    items = store.issues(
        instrument_id=ticker.upper() if ticker else None, check=check, severity=severity, limit=limit
    )
    return {"summary": store.issue_counts(), "count": len(items), "issues": items, "runs": store.runs(5)}


class MarketIngestRequest(BaseModel):
    tickers: list[str] | None = Field(default=None, max_length=50)
    start: str
    end: str
    yields: bool = True
    refresh: bool = False


@router.post("/ingest")
def ingest(
    req: MarketIngestRequest,
    request: Request,
    ctx: Ctx,
    x_admin_token: Annotated[str | None, Header()] = None,
) -> dict:
    token = ctx.settings.ingest_admin_token
    if token is None or not token.get_secret_value():
        raise HTTPException(403, "Ingestion over HTTP is disabled; use the CLI (market ingest).")
    if (request.client.host if request.client else "") not in LOOPBACK:
        raise HTTPException(403, "Ingestion is only accepted from the local machine.")
    if not x_admin_token or not hmac.compare_digest(x_admin_token, token.get_secret_value()):
        raise HTTPException(401, "Invalid or missing X-Admin-Token.")
    a, b = _date(req.start, "start"), _date(req.end, "end")
    if b < a or (b - a) > timedelta(days=366 * 5):
        raise HTTPException(422, "invalid range (max 5 years per request)")
    run = _run(lambda: MarketDataIngestor(ctx).run(req.tickers, a, b, refresh=req.refresh))
    out = {"run": run}
    if req.yields:
        out["yields"] = _run(lambda: YieldIngestor(ctx).run(None, a, b))
    return out
