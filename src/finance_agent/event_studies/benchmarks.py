"""Benchmark definitions: the primary benchmark is the caller's; alternatives are for robustness only."""

from __future__ import annotations

from finance_agent.event_studies.schemas import BenchmarkDefinition

DEFAULT_BENCHMARK = "SPY"
# Sector ETFs in the v0.3-A universe used as an ALTERNATIVE benchmark (never chosen by outcome).
SECTOR_BENCHMARK = {"MSFT": "XLK", "NVDA": "XLK"}
DESCRIPTIONS = {
    "SPY": "S&P 500 ETF (broad U.S. large-cap market proxy)",
    "QQQ": "Nasdaq-100 ETF (large-cap growth/technology proxy)",
    "XLK": "Technology Select Sector ETF (sector proxy; the target may be a large constituent)",
}


def definition(ticker: str) -> BenchmarkDefinition:
    t = ticker.upper()
    return BenchmarkDefinition(instrument_id=t, description=DESCRIPTIONS.get(t, f"{t} total-return series"))


def alternatives(target: str, primary: str | None) -> list[str]:
    cands = ["SPY", "QQQ", SECTOR_BENCHMARK.get(target.upper())]
    out = []
    for c in cands:
        if c and c != (primary or "").upper() and c != target.upper() and c not in out:
            out.append(c)
    return out
