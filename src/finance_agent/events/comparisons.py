"""Deterministic macro comparisons and the (consensus-gated) surprise engine.

Three different things are kept strictly apart:
  momentum   change in the variable itself (m/m, y/y, change in level)
  revision   a later vintage changing an already published value
  surprise   actual − market consensus; ONLY with an authenticated consensus observation
             that was published before the release. The previous value is never used
             as a stand-in for consensus.

All functions operate on one vintage (a point-in-time snapshot) and are pure.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import date, datetime

from finance_agent.events.schemas import Comparison, ConsensusStatus, Unit
from finance_agent.timeutil import ensure_aware

MIN_HISTORY = 24  # observations required for percentile / standardized statistics
HISTORY_WINDOW = 60
MIN_SURPRISE_HISTORY = 20


def _cid(*parts: object) -> str:
    return "CMP-" + hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:12]


def shift_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    return date(d.year + y, m + 1, 1)


def headline_series(levels: dict[date, float], measure: str, frequency: str) -> dict[date, float]:
    """Headline measure for every period in a vintage.

    measure: pct_change (m/m %), annualized_pct_change (q/q SAAR %), change (level diff), level.
    """
    step = 3 if frequency == "Q" else 1
    out: dict[date, float] = {}
    for d in sorted(levels):
        cur = levels[d]
        prev = levels.get(shift_months(d, -step))
        if cur is None:
            continue
        if measure == "level":
            out[d] = cur
        elif prev is None or (measure != "change" and prev == 0):
            continue
        elif measure == "pct_change":
            out[d] = (cur / prev - 1) * 100
        elif measure == "annualized_pct_change":
            out[d] = ((cur / prev) ** (12 / step) - 1) * 100
        elif measure == "change":
            out[d] = cur - prev
    return out


@dataclass(frozen=True)
class IndicatorSpec:
    series_id: str
    measure: str  # headline measure
    headline_unit: Unit
    level_unit: Unit
    frequency: str  # M | Q
    yoy: str  # pct (index/level series) | diff (rates)


def compute_comparisons(
    spec: IndicatorSpec, levels: dict[date, float], obs: date, vintage: str
) -> list[Comparison]:
    """Momentum comparisons for observation ``obs`` using one vintage's history."""
    step = 3 if spec.frequency == "Q" else 1
    ref = {"series_id": spec.series_id, "vintage_date": vintage}
    out: list[Comparison] = []
    cur = levels.get(obs)
    prev_d, yoy_d = shift_months(obs, -step), shift_months(obs, -12)
    prev, yago = levels.get(prev_d), levels.get(yoy_d)

    def add(kind, label, value, unit, formula, inputs, status="ok", note=None):
        out.append(
            Comparison(
                comparison_id=_cid(spec.series_id, vintage, obs, kind),
                kind=kind,
                label=label,
                value=value,
                unit=unit,
                formula=formula,
                inputs=inputs,
                status=status if value is not None else "unavailable",
                note=note,
            )
        )

    if cur is None:
        return out
    inp_cur = {**ref, "observation_date": obs.isoformat(), "value": cur}
    if prev is not None:
        inp_prev = {**ref, "observation_date": prev_d.isoformat(), "value": prev}
        add(
            "change_from_previous",
            "Change from previous period",
            cur - prev,
            Unit.PERCENTAGE_POINTS if spec.level_unit == Unit.PERCENT else spec.level_unit,
            "level[t] - level[t-1]",
            [inp_cur, inp_prev],
        )
        if spec.level_unit != Unit.PERCENT and prev != 0:
            if spec.frequency == "Q":
                add(
                    "annualized_pct_change",
                    "Annualized q/q % change",
                    ((cur / prev) ** 4 - 1) * 100,
                    Unit.PERCENT_CHANGE,
                    "((level[t]/level[t-1])^4 - 1) * 100",
                    [inp_cur, inp_prev],
                )
            else:
                add(
                    "pct_change_from_previous",
                    "Month-over-month % change",
                    (cur / prev - 1) * 100,
                    Unit.PERCENT_CHANGE,
                    "(level[t]/level[t-1] - 1) * 100",
                    [inp_cur, inp_prev],
                )
    if yago is not None:
        inp_y = {**ref, "observation_date": yoy_d.isoformat(), "value": yago}
        if spec.yoy == "pct" and yago != 0:
            add(
                "yoy_pct_change",
                "Year-over-year % change",
                (cur / yago - 1) * 100,
                Unit.PERCENT_CHANGE,
                "(level[t]/level[t-12m] - 1) * 100",
                [inp_cur, inp_y],
            )
        elif spec.yoy == "diff":
            add(
                "yoy_change",
                "Year-over-year change",
                cur - yago,
                Unit.PERCENTAGE_POINTS,
                "level[t] - level[t-12m]",
                [inp_cur, inp_y],
            )
    # Distributional context of the headline measure (prior observations only).
    hs = headline_series(levels, spec.measure, spec.frequency)
    x = hs.get(obs)
    history = [v for d, v in sorted(hs.items()) if d < obs][-HISTORY_WINDOW:]
    if x is not None:
        if len(history) >= MIN_HISTORY:
            pct = 100 * sum(1 for h in history if h <= x) / len(history)
            add(
                "percentile",
                f"Percentile of headline vs prior {len(history)} periods",
                pct,
                Unit.COUNT,
                "share of prior headline values <= current * 100",
                [{**ref, "n": len(history)}],
            )
            mean = sum(history) / len(history)
            sd = math.sqrt(sum((h - mean) ** 2 for h in history) / (len(history) - 1))
            if sd > 0:
                add(
                    "standardized_change",
                    f"Standardized headline vs prior {len(history)} periods",
                    (x - mean) / sd,
                    Unit.COUNT,
                    "(headline[t] - mean(prior)) / sd(prior)",
                    [{**ref, "n": len(history), "mean": mean, "sd": sd}],
                )
        else:
            add(
                "percentile",
                "Percentile of headline",
                None,
                Unit.COUNT,
                "requires history",
                [],
                note=f"Only {len(history)} prior observations (< {MIN_HISTORY}); not computed.",
            )
    return out


def revision_comparison(
    series_id: str, obs: date, old: float, new: float, old_vintage: str, new_vintage: str, unit: Unit
) -> Comparison:
    return Comparison(
        comparison_id=_cid(series_id, obs, old_vintage, new_vintage, "revision"),
        kind="revision",
        label="Revision to previously published value",
        value=new - old,
        unit=Unit.PERCENTAGE_POINTS if unit == Unit.PERCENT else unit,
        formula="value[new vintage] - value[previous vintage]",
        inputs=[
            {
                "series_id": series_id,
                "observation_date": obs.isoformat(),
                "vintage_date": old_vintage,
                "value": old,
            },
            {
                "series_id": series_id,
                "observation_date": obs.isoformat(),
                "vintage_date": new_vintage,
                "value": new,
            },
        ],
    )


@dataclass(frozen=True)
class ConsensusObservation:
    """An authenticated market-consensus forecast. None exists in v0.2 (no licensed source)."""

    value: float
    unit: Unit
    source_name: str
    source_url: str
    published_at: datetime


def surprise(
    actual: float,
    actual_unit: Unit,
    release_available_at: datetime,
    consensus: ConsensusObservation | None,
    historical_surprises: list[float] | None = None,
) -> tuple[ConsensusStatus, list[Comparison]]:
    """actual − consensus, and standardized surprise when a sufficient history exists.

    Returns ``unavailable`` (and no numbers) when there is no consensus, when it was
    published after the release became available (look-ahead), or when units differ.
    """
    if consensus is None:
        return ConsensusStatus.UNAVAILABLE, []
    if ensure_aware(consensus.published_at) >= ensure_aware(release_available_at):
        return ConsensusStatus.UNAVAILABLE, []
    if consensus.unit != actual_unit:
        return ConsensusStatus.UNAVAILABLE, []
    s = actual - consensus.value
    inputs = [
        {"actual": actual},
        {
            "consensus": consensus.value,
            "source": consensus.source_name,
            "published_at": consensus.published_at.isoformat(),
        },
    ]
    out = [
        Comparison(
            comparison_id=_cid("surprise", actual, consensus.value, consensus.source_name),
            kind="surprise",
            label="Actual minus consensus",
            value=s,
            unit=actual_unit,
            formula="actual - consensus",
            inputs=inputs,
        )
    ]
    if historical_surprises is not None and len(historical_surprises) >= MIN_SURPRISE_HISTORY:
        mean = sum(historical_surprises) / len(historical_surprises)
        sd = math.sqrt(sum((h - mean) ** 2 for h in historical_surprises) / (len(historical_surprises) - 1))
        if sd > 0:
            out.append(
                Comparison(
                    comparison_id=_cid("std_surprise", s, sd),
                    kind="standardized_surprise",
                    label="Standardized surprise",
                    value=s / sd,
                    unit=Unit.COUNT,
                    formula="surprise / sd(historical surprises)",
                    inputs=[{"n": len(historical_surprises), "sd": sd}],
                )
            )
    return ConsensusStatus.AVAILABLE, out
