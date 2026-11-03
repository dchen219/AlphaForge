"""Pure helpers for comparing API results at two cutoffs (no Streamlit, no arithmetic on values).

Each ``*_violations`` function checks a response against the cutoff *the server echoed back*:
anything returned with ``available_at`` later than that cutoff is a look-ahead defect and is
reported, never hidden. Diffs only line up values that the API returned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from finance_agent.interfaces.events_api import ReplayOut
from finance_agent.services import FinancialsResponse


def _aware(v: Any) -> datetime | None:
    if v is None:
        return None
    dt = datetime.fromisoformat(v) if isinstance(v, str) else v
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def replay_violations(res: ReplayOut) -> list[str]:
    bad = [
        f"visible {d.event.event_id} has available_at {d.event.available_at} > cutoff"
        for d in res.visible
        if d.event.available_at is None or d.event.available_at > res.as_of
    ]
    bad += [
        f"excluded {d.event.event_id} has available_at {d.event.available_at} <= cutoff"
        for d in res.excluded_future
        if d.event.available_at is not None and d.event.available_at <= res.as_of
    ]
    return bad


def financials_violations(data: FinancialsResponse) -> list[str]:
    cut = data.financials.as_of
    if cut is None:
        return []
    return [
        f"{v.metric} {v.period.label}: source {s.accession} available {s.available_at} > cutoff"
        for vals in data.financials.series.values()
        for v in vals
        if v is not None
        for s in v.sources
        if s.available_at > cut
    ]


def rows_violations(rows: list[dict], cutoff: datetime | None, label: str) -> list[str]:
    """Market bars / yield observations: ``available_at`` must not be after the cutoff."""
    if cutoff is None:
        return []
    cut = _aware(cutoff)
    return [
        f"{label} {r.get('session_date') or r.get('observation_date')}: available_at {r['available_at']} > cutoff"
        for r in rows
        if r.get("available_at") is not None and _aware(r["available_at"]) > cut
    ]


def diff_replay(a: ReplayOut, b: ReplayOut) -> list[dict]:
    """Events visible at each cutoff. ``newly visible`` = visible at B only."""
    va = {d.event.event_id: d.event for d in a.visible}
    vb = {d.event.event_id: d.event for d in b.visible}
    rows = []
    for k in sorted(set(va) | set(vb), key=lambda k: (va.get(k) or vb[k]).available_at or datetime.min):
        e = va.get(k) or vb[k]
        rows.append(
            {
                "event_id": k,
                "available_at": e.available_at,
                "category": str(e.event_type),
                "title": e.title,
                "visible at A": k in va,
                "visible at B": k in vb,
                "change": "unchanged" if k in va and k in vb else ("only at A" if k in va else "only at B"),
            }
        )
    return rows


def diff_financials(a: FinancialsResponse, b: FinancialsResponse) -> list[dict]:
    def index(d: FinancialsResponse) -> dict:
        f = d.financials
        return {(m, v.period.label): v for m, vals in f.series.items() for v in vals if v is not None}

    ia, ib = index(a), index(b)
    rows = []
    for key in sorted(set(ia) | set(ib)):
        x, y = ia.get(key), ib.get(key)
        rows.append(
            {
                "metric": key[0],
                "period": key[1],
                "value at A": x.value if x else None,
                "value at B": y.value if y else None,
                "unit": (x or y).unit,
                "filing at A": x.sources[0].accession if x and x.sources else None,
                "filing at B": y.sources[0].accession if y and y.sources else None,
                "restated at B": y.restated if y else None,
                "change": _change(
                    x.value if x else None, y.value if y else None, x is not None, y is not None
                ),
            }
        )
    return rows


def diff_rows(
    a: list[dict], b: list[dict], *, key: str, value: str, extra: tuple[str, ...] = ()
) -> list[dict]:
    """Align two row lists (bars or yield observations) on ``key``."""
    ia = {str(r.get(key)): r for r in a}
    ib = {str(r.get(key)): r for r in b}
    rows = []
    for k in sorted(set(ia) | set(ib)):
        x, y = ia.get(k), ib.get(k)
        vx = x.get(value) if x else None
        vy = y.get(value) if y else None
        row = {key: k, f"{value} at A": vx, f"{value} at B": vy}
        for e in extra:
            row[f"{e} at A"] = x.get(e) if x else None
            row[f"{e} at B"] = y.get(e) if y else None
        row["change"] = _change(vx, vy, vx is not None, vy is not None)
        rows.append(row)
    return rows


def _change(x: Any, y: Any, has_x: bool, has_y: bool) -> str:
    if has_x and has_y:
        return "unchanged" if x == y else "different"
    if has_y:
        return "only at B"
    if has_x:
        return "only at A"
    return "missing at both"


def summarize(rows: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r["change"]] = out.get(r["change"], 0) + 1
    return out
