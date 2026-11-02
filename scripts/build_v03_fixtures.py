"""Build v0.3-A offline fixtures.

1. ``--synthetic`` (default on): SYNTHETIC daily bars and corporate actions in the Alpaca Market
   Data response format for the 17-instrument universe. No market data is copied from any
   provider: prices are a seeded random walk, dividends/splits are invented schedules (the NVDA
   10-for-1 split date mirrors the publicly announced 2024 split so the split path is exercised
   on a realistic date, but every price is synthetic). Embedded edge cases:
     * IWM has no bar for 2025-03-14 (missing data),
     * XLE has a +30 % one-day move on 2025-08-14 (extreme move, must be kept and flagged),
     * the recording time is 2026-10-30 14:00 ET, so the 2026-10-30 bar is PROVISIONAL,
     * NVDA also has provider split-adjusted bars (adjustment=split) for reconciliation.
2. ``--record-yields``: records REAL keyless ALFRED vintages for DGS2/DGS10 (crawl-delay 2 s):
   one main vintage for ingestion plus consecutive-day vintages around holidays, used to verify
   the availability rule against real publication behaviour.

Usage:  uv run python scripts/build_v03_fixtures.py [--no-synthetic] [--record-yields]
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finance_agent.market.calendar import get_calendar  # noqa: E402
from finance_agent.market.universe import DEFAULT_UNIVERSE  # noqa: E402
from finance_agent.timeutil import ET  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
START, END = date(2024, 1, 2), date(2026, 10, 30)
RECORDED_AT = "2026-10-30T18:00:00+00:00"  # nominal synthetic "retrieval" time (14:00 ET Friday)

# Synthetic parameters: start price, annual drift, annual volatility, dividend schedule.
PARAMS = {
    "SPY": (100.0, 0.08, 0.15, "quarterly", 0.0030),
    "QQQ": (100.0, 0.10, 0.20, "quarterly", 0.0015),
    "IWM": (100.0, 0.05, 0.22, "quarterly", 0.0030),
    "IWF": (100.0, 0.10, 0.19, "quarterly", 0.0012),
    "IWD": (100.0, 0.06, 0.15, "quarterly", 0.0050),
    "SHY": (50.0, 0.00, 0.02, "monthly", 0.0030),
    "IEF": (50.0, -0.01, 0.07, "monthly", 0.0028),
    "TLT": (50.0, -0.02, 0.15, "monthly", 0.0035),
    "XLK": (100.0, 0.10, 0.22, "quarterly", 0.0016),
    "XLF": (40.0, 0.07, 0.18, "quarterly", 0.0040),
    "XLE": (40.0, 0.03, 0.25, "quarterly", 0.0080),
    "XLU": (40.0, 0.05, 0.16, "quarterly", 0.0075),
    "MSFT": (200.0, 0.10, 0.25, "quarterly", 0.0020),
    "NVDA": (50.0, 0.30, 0.50, "quarterly", 0.0001),
    "GOOGL": (100.0, 0.10, 0.28, "quarterly", 0.0012),
    "AMZN": (100.0, 0.12, 0.30, None, 0.0),
    "META": (200.0, 0.12, 0.35, "quarterly", 0.0010),
}
NVDA_SPLIT_EX = date(2024, 6, 10)
NVDA_SPLIT = 10.0
MISSING = {("IWM", date(2025, 3, 14))}
SHOCKS = {("XLE", date(2025, 8, 14)): 0.30}


def dividend_dates(kind: str | None, sessions: list[date]) -> list[date]:
    if kind is None:
        return []
    months = range(1, 13) if kind == "monthly" else (3, 6, 9, 12)
    out = []
    for y in (2024, 2025, 2026):
        for m in months:
            in_month = [s for s in sessions if s.year == y and s.month == m]
            if in_month:
                out.append(in_month[min(14, len(in_month) - 1)] if kind == "quarterly" else in_month[0])
    return out


def ts(d: date) -> str:
    """Alpaca daily-bar timestamp: 00:00 America/New_York of the session, expressed in UTC."""
    return datetime.combine(d, datetime.min.time(), tzinfo=ET).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def synth(ticker: str, sessions: list[date]) -> tuple[list[dict], list[dict], list[dict]]:
    p0, mu, sigma, div_kind, div_yield = PARAMS[ticker]
    rng = random.Random(f"AlphaForge-v0.3a-synthetic-{ticker}")
    divs = set(dividend_dates(div_kind, sessions))
    dt = 1 / 252
    econ = p0  # split-adjusted economic price path
    bars_raw, bars_split, actions_div = [], [], []
    prev_close_raw = None
    for d in sessions:
        z = rng.gauss(0, 1)
        econ *= math.exp((mu - 0.5 * sigma**2) * dt + sigma * math.sqrt(dt) * z)
        shock = SHOCKS.get((ticker, d))
        if shock:
            econ *= 1 + shock
        div_amt = 0.0
        if d in divs and prev_close_raw is not None:
            div_amt = round(econ * div_yield * (3 if div_kind == "quarterly" else 1), 4)
            econ -= div_amt
            actions_div.append(
                {
                    "id": f"SYN-DIV-{ticker}-{d.isoformat()}",
                    "symbol": ticker,
                    "cusip": None,
                    "rate": div_amt,
                    "special": False,
                    "foreign": False,
                    "process_date": (d + timedelta(days=30)).isoformat(),
                    "ex_date": d.isoformat(),
                    "record_date": (d + timedelta(days=1)).isoformat(),
                    "payable_date": (d + timedelta(days=21)).isoformat(),
                    "currency": "USD",
                }
            )
        scale = NVDA_SPLIT if (ticker == "NVDA" and d < NVDA_SPLIT_EX) else 1.0
        open_ = econ * math.exp(rng.gauss(0, sigma * math.sqrt(dt) * 0.3))
        hi = max(open_, econ) * (1 + abs(rng.gauss(0, sigma * math.sqrt(dt) * 0.5)))
        lo = min(open_, econ) * (1 - abs(rng.gauss(0, sigma * math.sqrt(dt) * 0.5)))
        vol = int(rng.lognormvariate(16, 0.4))
        bar_split = {
            "t": ts(d),
            "o": round(open_, 4),
            "h": round(hi, 4),
            "l": round(lo, 4),
            "c": round(econ, 4),
            "v": vol,
            "n": vol // 120,
            "vw": round((open_ + hi + lo + econ) / 4, 4),
        }
        bar_raw = {
            **bar_split,
            "o": round(open_ * scale, 2),
            "h": round(hi * scale, 2),
            "l": round(lo * scale, 2),
            "c": round(econ * scale, 2),
            "v": int(vol / scale),
            "vw": round((open_ + hi + lo + econ) / 4 * scale, 2),
        }
        prev_close_raw = bar_raw["c"]
        if (ticker, d) in MISSING:
            continue
        bars_raw.append(bar_raw)
        bars_split.append(bar_split)
    return bars_raw, bars_split, actions_div


def write_synthetic() -> None:
    cal = get_calendar()
    sessions = cal.sessions(START, END)
    for inst in DEFAULT_UNIVERSE:
        t = inst.instrument_id
        raw, split, divs = synth(t, sessions)
        for adj, rows in (("raw", raw), ("split", split)):
            if adj == "split" and t != "NVDA":
                continue
            p = FIX / "alpaca" / "bars" / adj / f"{t}.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(
                json.dumps({"bars": {t: rows}, "next_page_token": None}, separators=(",", ":")),
                encoding="utf-8",
            )
        actions: dict = {"cash_dividends": divs}
        if t == "NVDA":
            actions["forward_splits"] = [
                {
                    "id": "SYN-SPLIT-NVDA-2024-06-10",
                    "symbol": "NVDA",
                    "cusip": None,
                    "new_rate": NVDA_SPLIT,
                    "old_rate": 1.0,
                    "process_date": "2024-06-10",
                    "ex_date": NVDA_SPLIT_EX.isoformat(),
                    "record_date": "2024-06-06",
                    "payable_date": "2024-06-07",
                    "currency": "USD",
                }
            ]
        p = FIX / "alpaca" / "corporate_actions" / f"{t}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            json.dumps({"corporate_actions": actions, "next_page_token": None}, indent=1), encoding="utf-8"
        )
    print(f"synthetic Alpaca-format fixtures: {len(DEFAULT_UNIVERSE)} instruments, {len(sessions)} sessions")


# --------------------------------------------------------------------------- real ALFRED yields

MAIN_VINTAGE = date(2026, 11, 1)
YIELD_START = date(2024, 1, 2)
CALIBRATION_WINDOWS = [  # consecutive vintages around Federal Reserve / bond-market holidays
    (date(2025, 10, 9), date(2025, 10, 15)),  # Columbus Day 2025-10-13
    (date(2025, 11, 7), date(2025, 11, 13)),  # Veterans Day 2025-11-11
    (date(2025, 11, 25), date(2025, 12, 2)),  # Thanksgiving 2025-11-27 (+ early-close Friday)
    (date(2025, 12, 23), date(2025, 12, 30)),  # Christmas 2025-12-25
    (date(2026, 1, 15), date(2026, 1, 21)),  # Martin Luther King Jr. Day 2026-01-19
    (date(2026, 4, 1), date(2026, 4, 7)),  # Good Friday 2026-04-03 (Fed open)
    (date(2026, 6, 17), date(2026, 6, 23)),  # Juneteenth 2026-06-19
    (date(2026, 7, 1), date(2026, 7, 7)),  # Independence Day on Saturday 2026-07-04
    (date(2026, 10, 26), date(2026, 10, 31)),
]


def record_yields(user_agent: str) -> list[str]:
    out = []
    client = httpx.Client(headers={"User-Agent": user_agent}, timeout=30)
    jobs = [(s, MAIN_VINTAGE, YIELD_START, date(2026, 10, 31)) for s in ("DGS2", "DGS10")]
    for a, b in CALIBRATION_WINDOWS:
        d = a
        while d <= b:
            for s in ("DGS2", "DGS10"):
                jobs.append((s, d, a - timedelta(days=14), b))
            d += timedelta(days=1)
    for sid, vintage, cosd, coed in jobs:
        p = FIX / "alfred" / sid / f"{vintage.isoformat()}.csv"
        if p.exists():
            continue
        r = client.get(
            "https://alfred.stlouisfed.org/graph/alfredgraph.csv",
            params={
                "id": sid,
                "vintage_date": vintage.isoformat(),
                "cosd": cosd.isoformat(),
                "coed": coed.isoformat(),
            },
        )
        r.raise_for_status()
        if not r.text.startswith("observation_date"):
            raise SystemExit(f"unexpected ALFRED response for {sid} {vintage}: {r.text[:80]!r}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(r.content)
        out.append(p.relative_to(FIX).as_posix())
        time.sleep(2.0)  # robots.txt crawl-delay
    return out


def update_manifest(recorded_yields: bool) -> None:
    path = FIX / "MANIFEST.json"
    m = json.loads(path.read_text(encoding="utf-8"))
    m["groups"]["alpaca/"] = {
        "kind": "synthetic",
        "description": "SYNTHETIC daily bars and corporate actions in the Alpaca Market Data response "
        "format (seeded random walk, invented dividends; NVDA split date mirrors the public 2024 split "
        "but all prices are synthetic). NOT provider data; no live Alpaca response is committed.",
        "retrieved_at": RECORDED_AT,
        "built_by": "scripts/build_v03_fixtures.py",
    }
    if recorded_yields:
        m["yield_window"] = {
            "start": YIELD_START.isoformat(),
            "vintage": MAIN_VINTAGE.isoformat(),
            "note": "REAL ALFRED DGS2/DGS10 vintages; offline yield ingestion reads this vintage. "
            "Consecutive-day calibration vintages around holidays verify the availability rule.",
            "calibration_windows": [[a.isoformat(), b.isoformat()] for a, b in CALIBRATION_WINDOWS],
            "recorded_at": datetime.now(tz=UTC).replace(microsecond=0).isoformat(),
        }
    path.write_text(json.dumps(m, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-synthetic", action="store_true")
    ap.add_argument("--record-yields", action="store_true")
    ap.add_argument("--user-agent", default="AlphaForge/0.3 fixture builder")
    a = ap.parse_args()
    if not a.no_synthetic:
        write_synthetic()
    recorded = []
    if a.record_yields:
        recorded = record_yields(a.user_agent)
        print(f"recorded {len(recorded)} REAL ALFRED vintage files")
    update_manifest(bool(a.record_yields))


if __name__ == "__main__":
    main()
