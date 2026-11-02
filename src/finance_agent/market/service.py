"""Market-data queries with explicit output contracts.

Two query modes, deliberately different contracts:

* ``point_in_time`` (default): bars and corporate actions exactly as available at ``as_of``
  (default: now, the documented current-data policy). Corrections that arrived later and
  corporate actions not yet effective/known are invisible; adjusted prices use only the
  corporate actions available at the cutoff.
* ``research``: the latest stored version of every bar, adjusted with *all* corporate actions
  known now (retrospective). Such prices are NOT what an investor saw on those dates; the
  response says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from finance_agent.errors import DataUnavailableError, NotFoundError
from finance_agent.market import calculations as calc
from finance_agent.market.adjust import METHOD_VERSION, FactorSet, factors_for
from finance_agent.market.calendar import get_calendar
from finance_agent.market.quality import classify_gaps
from finance_agent.market.schemas import AdjustmentBasis, CorporateActionType
from finance_agent.market.store import MarketStore
from finance_agent.timeutil import ET, available_at, ensure_aware, utcnow

MODES = ("point_in_time", "research")
COMPUTED_BASES = (AdjustmentBasis.RAW, AdjustmentBasis.SPLIT_ADJUSTED, AdjustmentBasis.TOTAL_RETURN)


@dataclass
class PriceSeries:
    instrument_id: str
    basis: AdjustmentBasis
    mode: str
    as_of: datetime | None
    sessions: list[date]
    rows: list[dict]
    closes: dict[date, float]
    gaps: dict[date, str]
    factors: FactorSet
    split_ex_dates: list[date]
    data_origins: list[str]
    notes: list[str] = field(default_factory=list)

    def contract(self) -> dict:
        retro = self.mode == "research"
        return {
            "query_mode": self.mode,
            "as_of": self.as_of,
            "basis": str(self.basis),
            "adjustment_method": METHOD_VERSION if self.basis != AdjustmentBasis.RAW else None,
            "retrospectively_adjusted": retro and self.basis != AdjustmentBasis.RAW,
            "statement": (
                "RETROSPECTIVE research view: latest data adjusted with all corporate actions known "
                "now; prices are not necessarily what an investor saw on those dates."
                if retro
                else "POINT-IN-TIME view: only observations and corporate actions available at as_of."
            ),
        }


class MarketDataService:
    def __init__(self, db) -> None:
        self.store = MarketStore(db)
        self.calendar = get_calendar()

    def _instrument(self, ticker: str) -> str:
        t = ticker.upper()
        if t not in {i.instrument_id for i in self.store.instruments()}:
            raise NotFoundError(f"{t} is not in the market-data universe (run `market ingest` first)")
        return t

    def prices(
        self,
        ticker: str,
        start: date,
        end: date,
        *,
        basis: AdjustmentBasis = AdjustmentBasis.RAW,
        mode: str = "point_in_time",
        as_of: datetime | None = None,
        include_provisional: bool = False,
    ) -> PriceSeries:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if basis not in COMPUTED_BASES:
            raise ValueError(f"basis must be one of {[str(b) for b in COMPUTED_BASES]}")
        if mode == "research" and as_of is not None:
            raise ValueError("research mode is retrospective and does not take as_of; use point_in_time")
        if end < start:
            raise ValueError("end must not be before start")
        t = self._instrument(ticker)
        cut = ensure_aware(as_of) if as_of is not None else (utcnow() if mode == "point_in_time" else None)
        last_day = end if cut is None else min(end, cut.astimezone(ET).date())
        sessions = self.calendar.sessions(start, last_day) if last_day >= start else []
        if cut is not None and not include_provisional:
            # A session's daily bar cannot be final before D+1 00:00 New York: not a gap yet.
            sessions = [d for d in sessions if available_at(d) <= cut]
        raw = self.store.bars(
            t, adjustment="raw", start=start, end=end, as_of=cut, include_provisional=include_provisional
        )
        if not raw and not sessions:
            raise DataUnavailableError(f"no sessions for {t} in {start}..{end}")
        actions = [
            a
            for a in self.store.actions(t, as_of=cut)
            if cut is None or a.ex_date is None or a.ex_date <= cut.astimezone(ET).date()
        ]
        # Dividend factors need the raw closes before each ex-date, including before `start`.
        hist = self.store.bars(t, adjustment="raw", end=end, as_of=cut)
        factors = factors_for(basis, actions, {d: b.close for d, b in hist.items() if b.close})
        status = self.store.session_status(t, "raw", start, end)
        gaps = {d: str(k) for d, k in classify_gaps(sessions, raw, status, failed=False).items()}
        rows, closes = [], {}
        for d in sessions:
            b = raw.get(d)
            if b is None:
                rows.append({"session_date": d, "gap": gaps.get(d)})
                continue
            pm, vm = factors.price_multiplier(d), factors.volume_multiplier(d)
            row = {
                "session_date": d,
                "open": b.open * pm if b.open is not None else None,
                "high": b.high * pm if b.high is not None else None,
                "low": b.low * pm if b.low is not None else None,
                "close": b.close * pm if b.close is not None else None,
                "volume": b.volume * vm if b.volume is not None else None,
                "price_factor": pm,
                "bar_status": str(b.bar_status),
                "quality_status": str(b.quality_status),
                "version": b.version,
                "provider": b.provider,
                "provider_symbol": b.provider_symbol,
                "available_at": b.available_at,
                "availability_precision": str(b.availability_precision),
                "retrieved_at": b.retrieved_at,
                "snapshot_sha256": b.snapshot_sha256,
                "data_origin": b.data_origin,
            }
            rows.append(row)
            if row["close"] is not None and str(b.bar_status) == "FINAL":
                closes[d] = row["close"]
        splits = sorted(
            a.ex_date for a in actions if a.action_type == CorporateActionType.STOCK_SPLIT and a.ex_date
        )
        notes = list(factors.notes)
        if include_provisional and any(r.get("bar_status") == "PROVISIONAL" for r in rows):
            notes.append("PROVISIONAL bars included for display only; they are excluded from calculations.")
        return PriceSeries(
            t, basis, mode, cut, sessions, rows, closes, gaps, factors, splits,
            sorted({r["data_origin"] for r in rows if r.get("data_origin")}), notes,
        )  # fmt: skip

    def returns(
        self,
        ticker: str,
        start: date,
        end: date,
        *,
        basis: AdjustmentBasis,
        kind: str = "simple",
        mode: str = "point_in_time",
        as_of: datetime | None = None,
        vol_window: int = 20,
    ) -> dict:
        ps = self.prices(ticker, start, end, basis=basis, mode=mode, as_of=as_of)
        blocked = {}
        if basis == AdjustmentBasis.RAW:
            for d in ps.split_ex_dates:
                blocked[d] = (
                    "raw price change across a split ex-date is not an economic return; "
                    "use basis=split_adjusted or total_return"
                )
        fn = calc.log_returns if kind == "log" else calc.simple_returns
        pts = fn(ps.closes, ps.sessions, blocked)
        simple = pts if kind != "log" else calc.simple_returns(ps.closes, ps.sessions, blocked)
        cumulative, gaps = calc.cumulative_return(simple)
        logs = pts if kind == "log" else calc.log_returns(ps.closes, ps.sessions, blocked)
        vol = calc.rolling_volatility(logs, vol_window) if len(logs) >= vol_window else []
        dd, max_dd = calc.drawdowns(ps.closes, ps.sessions)
        notes = list(ps.notes)
        if basis == AdjustmentBasis.RAW and any(
            ps.sessions and ps.sessions[0] < d <= ps.sessions[-1] for d in blocked
        ):
            dd, max_dd = [], None
            notes.append("drawdown not computed on the raw basis across a split ex-date; use split_adjusted")
        return {
            "instrument_id": ps.instrument_id,
            "contract": ps.contract(),
            "kind": kind,
            "returns": [{"session_date": p.session, "value": p.value, "note": p.note} for p in pts],
            "cumulative_return": cumulative,
            "cumulative_note": None if cumulative is not None else "chain broken: " + "; ".join(gaps[:5]),
            "rolling_volatility": {
                "window": vol_window,
                "annualization": calc.TRADING_DAYS_PER_YEAR,
                "points": [{"session_date": p.session, "value": p.value, "note": p.note} for p in vol],
            },
            "max_drawdown": max_dd,
            "drawdown_last": dd[-1].value if dd else None,
            "missing_sessions": {str(d): k for d, k in ps.gaps.items()},
            "data_origins": ps.data_origins,
            "notes": notes,
        }

    def correlations(
        self,
        tickers: list[str],
        start: date,
        end: date,
        *,
        basis: AdjustmentBasis,
        mode: str = "point_in_time",
        as_of: datetime | None = None,
        min_obs: int = 20,
    ) -> dict:
        series = {}
        for t in tickers:
            ps = self.prices(t, start, end, basis=basis, mode=mode, as_of=as_of)
            blocked = (
                {d: "split ex-date (raw basis)" for d in ps.split_ex_dates}
                if basis == AdjustmentBasis.RAW
                else None
            )
            series[ps.instrument_id] = calc.simple_returns(ps.closes, ps.sessions, blocked)
        names = list(series)
        matrix = {}
        for a in names:
            for b in names:
                rho, n, note = calc.correlation(series[a], series[b], min_obs)
                matrix[f"{a}|{b}"] = {"correlation": rho, "observations": n, "note": note}
        return {"basis": str(basis), "mode": mode, "as_of": as_of, "instruments": names, "pairs": matrix}
