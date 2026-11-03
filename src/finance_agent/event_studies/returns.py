"""Daily returns for event studies, built ONLY from the v0.3-A market-data service.

No normalization or adjustment is re-implemented here: prices come from
``MarketDataService.prices`` (explicit basis, query mode, as-of cutoff) and returns from
``market.calculations`` (consecutive sessions only; raw returns across a split ex-date refused).
Provisional and quarantined bars are excluded by the service; missing sessions stay missing.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime

from finance_agent.errors import DataUnavailableError, NotFoundError
from finance_agent.event_studies.schemas import DataMode, ReturnType, sha256_json
from finance_agent.market import calculations as calc
from finance_agent.market.schemas import AdjustmentBasis
from finance_agent.market.service import MarketDataService


@dataclass
class ReturnSeries:
    instrument_id: str
    values: dict[date, float | None]  # session -> return (None = unavailable)
    reasons: dict[date, str]
    manifest: dict
    data_origins: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def get(self, d: date) -> float | None:
        return self.values.get(d)

    def reason(self, d: date) -> str:
        return self.reasons.get(d) or "no return for this session"


def fetch_returns(
    service: MarketDataService,
    ticker: str,
    sessions: list[date],
    *,
    basis: str,
    return_type: ReturnType,
    mode: DataMode,
    as_of: datetime | None,
) -> ReturnSeries:
    """Returns on every session in ``sessions[1:]`` (``sessions[0]`` supplies the first prior close)."""
    b = AdjustmentBasis(basis)
    try:
        ps = service.prices(
            ticker, sessions[0], sessions[-1], basis=b, mode=str(mode),
            as_of=as_of if mode == DataMode.POINT_IN_TIME else None,
        )  # fmt: skip
    except (DataUnavailableError, NotFoundError) as exc:
        return ReturnSeries(
            ticker.upper(), {}, {d: str(exc) for d in sessions},
            {"instrument_id": ticker.upper(), "error": str(exc)},
        )  # fmt: skip
    blocked = (
        {d: "raw price change across a split ex-date is not an economic return" for d in ps.split_ex_dates}
        if b == AdjustmentBasis.RAW
        else None
    )
    fn = calc.log_returns if return_type == ReturnType.LOG else calc.simple_returns
    pts = fn(ps.closes, ps.sessions, blocked)
    values: dict[date, float | None] = {}
    reasons: dict[date, str] = {}
    available = set(ps.sessions)
    for p in pts:
        values[p.session] = p.value
        if p.value is None:
            gap = ps.gaps.get(p.session)
            reasons[p.session] = f"{p.note}" + (f" ({gap})" if gap else "")
    for d in sessions[1:]:
        if d not in available:
            values[d] = None
            reasons[d] = (
                "not available at the decision cutoff"
                if mode == DataMode.POINT_IN_TIME
                else "session outside the stored data"
            )
        elif d not in values:
            values[d] = None
            reasons[d] = "no prior session in range"
    for d, v in list(values.items()):
        if v is not None and not math.isfinite(v):
            values[d], reasons[d] = None, "non-finite return"
    used = [r for r in ps.rows if r.get("close") is not None]
    retrieved = [r["retrieved_at"] for r in used if r.get("retrieved_at")]
    manifest = {
        "instrument_id": ps.instrument_id,
        "basis": str(b),
        "return_type": str(return_type),
        **ps.contract(),
        "bars_used": len(used),
        "first_session": str(sessions[0]),
        "last_session": str(sessions[-1]),
        "bar_versions": sorted({r["version"] for r in used}),
        "corrected_sessions": [str(r["session_date"]) for r in used if (r.get("version") or 1) > 1],
        "snapshot_sha256": sorted({r["snapshot_sha256"] for r in used if r.get("snapshot_sha256")}),
        "providers": sorted({r["provider"] for r in used if r.get("provider")}),
        "data_origins": ps.data_origins,
        "latest_retrieved_at": max(retrieved).isoformat() if retrieved else None,
        "adjustment_factors": [
            {
                "ex_date": str(f.ex_date),
                "price_factor": f.price_factor,
                "method": f.method,
                "source_action_id": f.source_action_id,
            }
            for f in ps.factors.factors
        ],
        "split_ex_dates": [str(d) for d in ps.split_ex_dates],
        "bars_digest": sha256_json(
            [
                (
                    str(r["session_date"]),
                    r["version"],
                    r["close"],
                    r["price_factor"],
                    r.get("snapshot_sha256"),
                )
                for r in used
            ]
        ),
    }
    if mode == DataMode.POINT_IN_TIME and as_of is not None:
        late = [r for r in used if r.get("retrieved_at") and r["retrieved_at"] > as_of]
        manifest["point_in_time_verified"] = not late
        manifest["bars_retrieved_after_cutoff"] = len(late)
    return ReturnSeries(ps.instrument_id, values, reasons, manifest, ps.data_origins, list(ps.notes))
