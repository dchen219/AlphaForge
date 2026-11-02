"""Deterministic market calculations (scalar Python; no estimation, no prediction).

Return rules
* A return is computed only between two *consecutive trading sessions* that both have a valid
  value. If the previous session is missing (missing data, quarantined, not finalized), the
  return is None with a reason — it is never bridged with a nearby price.
* The caller states the price basis; RAW returns across a split ex-date are refused (None),
  because the raw price change is not an economic return.
* Cumulative return requires an unbroken chain; otherwise None with the gap listed.
* Rolling volatility = sample standard deviation of daily log returns × sqrt(252), computed
  only when the window has ``window`` valid returns.
* Yield changes are in basis points: (y_t − y_prev) × 100 (1 percentage point = 100 bp).
"""

from __future__ import annotations

import itertools
import math
import statistics
from dataclasses import dataclass
from datetime import date

TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class Point:
    session: date
    value: float | None
    note: str | None = None


def _returns(
    values: dict[date, float],
    sessions: list[date],
    blocked: dict[date, str] | None,
    fn,
) -> list[Point]:
    out: list[Point] = []
    for prev, cur in itertools.pairwise(sessions):
        if blocked and cur in blocked:
            out.append(Point(cur, None, blocked[cur]))
        elif values.get(prev) is None:
            out.append(Point(cur, None, f"no valid value for previous session {prev}"))
        elif values.get(cur) is None:
            out.append(Point(cur, None, f"no valid value for session {cur}"))
        elif values[prev] <= 0 or values[cur] <= 0:
            out.append(Point(cur, None, "non-positive price"))
        else:
            out.append(Point(cur, fn(values[prev], values[cur])))
    return out


def simple_returns(
    values: dict[date, float], sessions: list[date], blocked: dict[date, str] | None = None
) -> list[Point]:
    """r_t = P_t / P_{t-1} − 1 over consecutive sessions."""
    return _returns(values, sessions, blocked, lambda a, b: b / a - 1.0)


def log_returns(
    values: dict[date, float], sessions: list[date], blocked: dict[date, str] | None = None
) -> list[Point]:
    """r_t = ln(P_t / P_{t-1}) over consecutive sessions."""
    return _returns(values, sessions, blocked, lambda a, b: math.log(b / a))


def cumulative_return(simple: list[Point]) -> tuple[float | None, list[str]]:
    gaps = [f"{p.session}: {p.note}" for p in simple if p.value is None]
    if gaps or not simple:
        return None, gaps or ["no returns"]
    total = 1.0
    for p in simple:
        total *= 1.0 + p.value
    return total - 1.0, []


def rolling_volatility(logs: list[Point], window: int) -> list[Point]:
    if window < 2:
        raise ValueError("window must be >= 2")
    out = []
    for i in range(len(logs)):
        if i + 1 < window:
            continue
        chunk = logs[i + 1 - window : i + 1]
        if any(p.value is None for p in chunk):
            out.append(Point(logs[i].session, None, f"window contains a missing return (needs {window})"))
        else:
            sd = statistics.stdev(p.value for p in chunk)
            out.append(Point(logs[i].session, sd * math.sqrt(TRADING_DAYS_PER_YEAR)))
    return out


def drawdowns(values: dict[date, float], sessions: list[date]) -> tuple[list[Point], float | None]:
    """Drawdown from the running peak of the given (basis-labelled) price series."""
    peak, out, worst = None, [], None
    for s in sessions:
        v = values.get(s)
        if v is None:
            out.append(Point(s, None, "missing value"))
            continue
        peak = v if peak is None else max(peak, v)
        dd = v / peak - 1.0
        worst = dd if worst is None else min(worst, dd)
        out.append(Point(s, dd))
    return out, worst


def correlation(a: list[Point], b: list[Point], min_obs: int = 20) -> tuple[float | None, int, str | None]:
    """Pearson correlation over sessions where both returns are valid."""
    bv = {p.session: p.value for p in b if p.value is not None}
    pairs = [(p.value, bv[p.session]) for p in a if p.value is not None and p.session in bv]
    if len(pairs) < min_obs:
        return None, len(pairs), f"only {len(pairs)} paired observations (< {min_obs})"
    xs, ys = zip(*pairs, strict=True)
    try:
        return statistics.correlation(xs, ys), len(pairs), None
    except statistics.StatisticsError as exc:
        return None, len(pairs), str(exc)


def yield_changes_bp(observations: list[tuple[date, float | None]]) -> list[dict]:
    """Change vs the previous *published* observation, in basis points.

    Missing observations (FRED '.') are reported, not filled; the next change is measured
    against the last published value and states that date.
    """
    out, prev = [], None
    for d, v in sorted(observations):
        if v is None:
            out.append({"date": d, "yield_pct": None, "change_bp": None, "note": "no observation published"})
            continue
        if prev is None:
            out.append({"date": d, "yield_pct": v, "change_bp": None, "note": "first observation in range"})
        else:
            out.append(
                {
                    "date": d,
                    "yield_pct": v,
                    "change_bp": round((v - prev[1]) * 100.0, 10),
                    "previous_date": prev[0],
                    "note": None,
                }
            )
        prev = (d, v)
    return out
