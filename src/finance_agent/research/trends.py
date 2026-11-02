"""Deterministic trend statistics over calculation series, and template findings."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from pydantic import BaseModel, Field

from finance_agent.research.formatting import fmt_pp, fmt_value
from finance_agent.schemas.financials import CalculationResult, QuarterlyFinancials


class TrendResult(BaseModel):
    trend_id: str
    metric: str
    label: str
    unit: str
    first_period: str
    last_period: str
    first_value: float
    last_value: float
    change: float  # last - first (ratio points for ratios, USD for amounts)
    change_unit: str  # ratio_change | USD
    min_value: float
    min_period: str
    max_value: float
    max_period: str
    mean_value: float
    observations: int
    direction: str  # up | down | flat
    input_ids: list[str]
    warnings: list[str] = Field(default_factory=list)


TREND_METRICS = (
    "revenue_yoy",
    "gross_margin",
    "operating_margin",
    "net_margin",
    "free_cash_flow",
    "fcf_margin",
    "capex_to_revenue",
    "capex_yoy",
    "ocf_to_capex",
)
FLAT_THRESHOLD = {"ratio": 0.005, "multiple": 0.05, "USD": 0.0}


def compute_trends(calcs: list[CalculationResult]) -> list[TrendResult]:
    by_name: dict[str, list[CalculationResult]] = defaultdict(list)
    for c in calcs:
        if c.name in TREND_METRICS and c.status == "ok" and c.value is not None:
            by_name[c.name].append(c)
    out: list[TrendResult] = []
    for name in TREND_METRICS:
        series = sorted(by_name.get(name, []), key=lambda c: c.period_end)
        if len(series) < 2:
            continue
        first, last = series[0], series[-1]
        vals = [c.value for c in series]  # type: ignore[misc]
        lo = min(series, key=lambda c: c.value)  # type: ignore[arg-type, return-value]
        hi = max(series, key=lambda c: c.value)  # type: ignore[arg-type, return-value]
        change = last.value - first.value  # type: ignore[operator]
        thr = FLAT_THRESHOLD.get(first.unit, 0.0)
        if first.unit == "USD":
            thr = 0.02 * abs(first.value or 0)  # type: ignore[arg-type]
        direction = "flat" if abs(change) <= thr else ("up" if change > 0 else "down")
        ids = [c.calc_id for c in series]
        warnings = []
        expected = len(set(c.period_label for c in series))
        if expected != len(series):
            warnings.append("Duplicate periods in trend input.")
        out.append(
            TrendResult(
                trend_id="T-" + hashlib.sha256(("|".join([name, *ids])).encode()).hexdigest()[:12],
                metric=name,
                label=first.label,
                unit=first.unit,
                first_period=first.period_label,
                last_period=last.period_label,
                first_value=first.value,
                last_value=last.value,  # type: ignore[arg-type]
                change=change,
                change_unit="ratio_change" if first.unit == "ratio" else first.unit,
                min_value=lo.value,
                min_period=lo.period_label,  # type: ignore[arg-type]
                max_value=hi.value,
                max_period=hi.period_label,  # type: ignore[arg-type]
                mean_value=sum(vals) / len(vals),
                observations=len(series),
                direction=direction,
                input_ids=ids,
                warnings=warnings,
            )
        )
    return out


class Finding(BaseModel):
    finding_id: str
    section: str  # growth | margins | cash_flow | capital | summary
    text: str
    evidence_ids: list[str]
    kind: str = "verified_fact"  # verified_fact | model_interpretation


_SECTION = {
    "revenue_yoy": "growth",
    "gross_margin": "margins",
    "operating_margin": "margins",
    "net_margin": "margins",
    "free_cash_flow": "cash_flow",
    "fcf_margin": "cash_flow",
    "capex_to_revenue": "capital",
    "capex_yoy": "capital",
    "ocf_to_capex": "capital",
}
_VERB = {"up": "rose", "down": "declined", "flat": "was broadly stable"}


def template_findings(trends: list[TrendResult], fin: QuarterlyFinancials) -> list[Finding]:
    """Deterministic, template-based findings. Every number comes from a trend."""
    findings: list[Finding] = []
    for t in trends:
        first = fmt_value(t.first_value, t.unit)
        last = fmt_value(t.last_value, t.unit)
        if t.unit == "ratio":
            delta = f"a change of {fmt_pp(t.change)}"
        else:
            delta = f"a change of {fmt_value(t.change, t.unit)}"
        text = (
            f"{t.label} {_VERB[t.direction]} from {first} in {t.first_period} to {last} in "
            f"{t.last_period} ({delta}); range {fmt_value(t.min_value, t.unit)} "
            f"({t.min_period}) to {fmt_value(t.max_value, t.unit)} ({t.max_period}) over "
            f"{t.observations} quarters."
        )
        findings.append(
            Finding(
                finding_id=f"FND-{len(findings) + 1}",
                section=_SECTION[t.metric],
                text=text,
                evidence_ids=[t.trend_id],
            )
        )
    return findings
