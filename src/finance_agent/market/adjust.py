"""Price-adjustment factors derived from stored corporate actions (documented, reproducible).

Conventions (backward adjustment; the latest prices are left unchanged):

* Split with ratio r = new_rate / old_rate (10-for-1 forward split: r = 10; 1-for-10 reverse:
  r = 0.1). Every session strictly before the ex-date is multiplied by 1 / r (prices) and r
  (volume).
* Cash dividend D with ex-date E: every session strictly before E is multiplied by
  f = 1 − D / C, where C is the RAW close of the last session before E (CRSP-style
  total-return factor). D is per share as of the ex-date, so C is first split-adjusted for any
  split whose ex-date falls after that session and on/before E.
* The total-return basis applies split and dividend factors; the split-adjusted basis applies
  split factors only. RAW is never adjusted.

Factors are only computed from actions supplied by the caller: point-in-time queries pass only
actions available at the cutoff, so a future split never adjusts an earlier view. Missing
inputs (e.g. no close before a dividend ex-date) produce no factor and an explicit note —
nothing is estimated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from finance_agent.market.schemas import (
    AdjustmentBasis,
    AdjustmentFactor,
    CorporateAction,
    CorporateActionType,
)

METHOD_VERSION = "adjustment-v0.3a.1"


@dataclass
class FactorSet:
    factors: list[AdjustmentFactor] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def price_multiplier(self, session: date) -> float:
        m = 1.0
        for f in self.factors:
            if session < f.ex_date:
                m *= f.price_factor
        return m

    def volume_multiplier(self, session: date) -> float:
        m = 1.0
        for f in self.factors:
            if session < f.ex_date:
                m *= f.volume_factor
        return m


def split_factors(actions: list[CorporateAction]) -> FactorSet:
    out = FactorSet()
    for a in sorted(actions, key=lambda a: (a.ex_date or date.min, a.action_id)):
        if a.action_type != CorporateActionType.STOCK_SPLIT:
            continue
        r = a.split_ratio
        if not r or a.ex_date is None:
            out.notes.append(f"split {a.action_id} skipped: invalid ratio or ex-date")
            continue
        out.factors.append(
            AdjustmentFactor(
                instrument_id=a.instrument_id,
                ex_date=a.ex_date,
                basis=AdjustmentBasis.SPLIT_ADJUSTED,
                price_factor=1.0 / r,
                volume_factor=r,
                source_action_id=a.action_id,
                source_provider=a.provider,
                method=f"split {a.new_rate:g}-for-{a.old_rate:g}: prices × old/new, volume × new/old",
            )
        )
    return out


def total_return_factors(actions: list[CorporateAction], raw_closes: dict[date, float]) -> FactorSet:
    """Split factors plus CRSP-style dividend factors (needs raw closes)."""
    out = split_factors(actions)
    splits = list(out.factors)
    sessions = sorted(raw_closes)
    for a in sorted(actions, key=lambda a: (a.ex_date or date.min, a.action_id)):
        if a.action_type != CorporateActionType.CASH_DIVIDEND:
            continue
        if a.ex_date is None or a.cash_amount is None:
            out.notes.append(f"dividend {a.action_id} skipped: missing ex-date or amount")
            continue
        prior = [s for s in sessions if s < a.ex_date]
        if not prior:
            out.notes.append(
                f"dividend {a.action_id} (ex {a.ex_date}) not applied: no raw close before the ex-date"
            )
            continue
        ref_session = prior[-1]
        close = raw_closes[ref_session]
        # Express the reference close in ex-date share terms (splits between it and the ex-date).
        for s in splits:
            if ref_session < s.ex_date <= a.ex_date:
                close *= s.price_factor
        if close <= 0 or a.cash_amount >= close:
            out.notes.append(
                f"dividend {a.action_id} (ex {a.ex_date}) not applied: amount {a.cash_amount} "
                f"is not below the reference close {close}"
            )
            continue
        out.factors.append(
            AdjustmentFactor(
                instrument_id=a.instrument_id,
                ex_date=a.ex_date,
                basis=AdjustmentBasis.TOTAL_RETURN,
                price_factor=1.0 - a.cash_amount / close,
                volume_factor=1.0,
                source_action_id=a.action_id,
                source_provider=a.provider,
                method="dividend: 1 - D / close(last session before ex-date)",
                reference_close=close,
                reference_session=ref_session,
            )
        )
    return out


def factors_for(
    basis: AdjustmentBasis, actions: list[CorporateAction], raw_closes: dict[date, float]
) -> FactorSet:
    if basis == AdjustmentBasis.RAW:
        return FactorSet()
    if basis == AdjustmentBasis.SPLIT_ADJUSTED:
        return split_factors(actions)
    if basis == AdjustmentBasis.TOTAL_RETURN:
        return total_return_factors(actions, raw_closes)
    raise ValueError(f"{basis} is a provider-supplied basis and is never re-adjusted internally")


def reconcile(internal: dict[date, float], provider: dict[date, float], *, rel_tol: float = 1e-4) -> dict:
    """Compare an internally adjusted close series with a provider-adjusted one."""
    common = sorted(set(internal) & set(provider))
    diffs = [
        (d, abs(internal[d] - provider[d]) / abs(provider[d])) for d in common if provider[d] not in (0, None)
    ]
    worst = max(diffs, key=lambda x: x[1], default=(None, 0.0))
    bad = [d for d, x in diffs if x > rel_tol]
    return {
        "sessions_compared": len(diffs),
        "max_relative_difference": worst[1],
        "worst_session": worst[0].isoformat() if worst[0] else None,
        "mismatched_sessions": len(bad),
        "within_tolerance": not bad and bool(diffs),
        "tolerance": rel_tol,
    }
