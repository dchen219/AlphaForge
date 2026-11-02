"""Deterministic financial calculations with provenance and compatibility checks.

Every function returns a :class:`CalculationResult`. When inputs are missing,
incompatible (unit/period) or the denominator is zero, the result has
``status="unavailable"`` and ``value=None`` — values are never fabricated.

Formulas
- growth (YoY / QoQ)     = (current − prior) / |prior|
- gross margin           = GrossProfit / Revenue
                           (fallback: (Revenue − CostOfRevenue) / Revenue, flagged)
- operating margin       = OperatingIncome / Revenue
- net margin             = NetIncome / Revenue
- free cash flow         = OperatingCashFlow − CAPEX   (CAPEX: see concepts.py)
- FCF margin             = FCF / Revenue
- CAPEX intensity        = CAPEX / Revenue
- OCF coverage of CAPEX  = OperatingCashFlow / CAPEX
- net debt               = TotalDebt − CashAndCashEquivalents
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass
from datetime import date

from finance_agent.schemas.financials import (
    CalcInput,
    CalculationResult,
    NormalizedValue,
    QuarterlyFinancials,
)

DURATION_TOLERANCE_DAYS = 7


@dataclass(frozen=True)
class Operand:
    name: str
    value: float
    unit: str
    ref_id: str
    period_label: str
    start: date | None
    end: date
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None

    @property
    def duration(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days + 1


def operand(name: str, x: NormalizedValue | CalculationResult | None) -> Operand | None:
    if x is None:
        return None
    if isinstance(x, NormalizedValue):
        return Operand(
            name,
            x.value,
            x.unit,
            x.value_id,
            x.period.label,
            x.period.start,
            x.period.end,
            x.period.fiscal_year,
            x.period.fiscal_quarter,
        )
    if x.value is None or x.status != "ok":
        return None
    return Operand(name, x.value, x.unit, x.calc_id, x.period_label, x.period_start, x.period_end)


def _calc_id(name: str, period_label: str, inputs: list[Operand]) -> str:
    key = f"{name}|{period_label}|" + ",".join(f"{o.ref_id}={o.value!r}" for o in inputs)
    return "C-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _result(
    name: str,
    label: str,
    formula: str,
    unit: str,
    ops: list[Operand | None],
    value: float | None,
    warnings: list[str],
    anchor: Operand | None,
    period_label: str | None = None,
) -> CalculationResult:
    present = [o for o in ops if o is not None]
    pl = period_label or (anchor.period_label if anchor else "n/a")
    return CalculationResult(
        calc_id=_calc_id(name, pl, present),
        name=name,
        label=label,
        period_label=pl,
        period_start=anchor.start if anchor else None,
        period_end=anchor.end if anchor else date.min,
        value=value,
        unit=unit,
        formula=formula,
        inputs=[
            CalcInput(name=o.name, value=o.value, unit=o.unit, ref_id=o.ref_id, period_label=o.period_label)
            for o in present
        ],
        warnings=warnings,
        status="ok" if value is not None else "unavailable",
    )


def _missing(ops: dict[str, Operand | None]) -> list[str]:
    return [f"Missing input: {k}." for k, v in ops.items() if v is None]


def ratio(
    name: str,
    label: str,
    num: Operand | None,
    den: Operand | None,
    formula: str,
    *,
    warn_negative_den: bool = True,
) -> CalculationResult:
    warnings = _missing({num.name if num else "numerator": num, den.name if den else "denominator": den})
    value = None
    anchor = num or den
    if num and den:
        if num.unit != den.unit:
            warnings.append(f"Unit mismatch: {num.unit} vs {den.unit}.")
        elif (num.start, num.end) != (den.start, den.end):
            warnings.append(
                f"Period mismatch: {num.period_label} {num.start}..{num.end} vs {den.start}..{den.end}."
            )
        elif den.value == 0:
            warnings.append("Denominator is zero; ratio undefined.")
        else:
            value = num.value / den.value
            if den.value < 0 and warn_negative_den:
                warnings.append("Denominator is negative; ratio sign is not economically intuitive.")
    return _result(name, label, formula, "ratio", [num, den], value, warnings, anchor)


def difference(
    name: str, label: str, a: Operand | None, b: Operand | None, formula: str, *, instant: bool = False
) -> CalculationResult:
    warnings = _missing({a.name if a else "minuend": a, b.name if b else "subtrahend": b})
    value = None
    if a and b:
        if a.unit != b.unit:
            warnings.append(f"Unit mismatch: {a.unit} vs {b.unit}.")
        elif (a.end != b.end) if instant else ((a.start, a.end) != (b.start, b.end)):
            warnings.append(f"Period mismatch: {a.start}..{a.end} vs {b.start}..{b.end}.")
        else:
            value = a.value - b.value
    return _result(
        name, label, formula, a.unit if a else (b.unit if b else "USD"), [a, b], value, warnings, a or b
    )


def growth(
    name: str, label: str, cur: Operand | None, prior: Operand | None, formula: str, *, expect: str
) -> CalculationResult:
    """``expect`` is 'yoy' or 'qoq' and is used to verify period comparability."""
    warnings = _missing({"current": cur, "prior": prior})
    value = None
    if cur and prior:
        if cur.unit != prior.unit:
            warnings.append(f"Unit mismatch: {cur.unit} vs {prior.unit}.")
        elif not _comparable(cur, prior, expect, warnings):
            pass
        elif prior.value == 0:
            warnings.append("Prior-period value is zero; growth undefined.")
        else:
            value = (cur.value - prior.value) / abs(prior.value)
            if prior.value < 0:
                warnings.append("Prior-period value is negative; growth computed against |prior|.")
            if cur.duration and prior.duration and abs(cur.duration - prior.duration) > 0:
                warnings.append(f"Period lengths differ ({cur.duration}d vs {prior.duration}d).")
    return _result(name, label, formula, "ratio", [cur, prior], value, warnings, cur)


def _comparable(cur: Operand, prior: Operand, expect: str, warnings: list[str]) -> bool:
    if cur.duration and prior.duration and abs(cur.duration - prior.duration) > DURATION_TOLERANCE_DAYS:
        warnings.append(f"Non-comparable period lengths ({cur.duration}d vs {prior.duration}d).")
        return False
    if cur.fiscal_year is None or prior.fiscal_year is None:
        return True
    if expect == "yoy":
        ok = prior.fiscal_year == cur.fiscal_year - 1 and prior.fiscal_quarter == cur.fiscal_quarter
    else:
        cy, cq = cur.fiscal_year, cur.fiscal_quarter or 0
        ok = (prior.fiscal_year, prior.fiscal_quarter) == ((cy, cq - 1) if cq > 1 else (cy - 1, 4))
    if not ok:
        warnings.append(
            f"Prior period {prior.period_label} is not the expected {expect.upper()} "
            f"comparison for {cur.period_label}."
        )
    return ok


# ---------------------------------------------------------------- orchestration


def compute_quarterly_metrics(fin: QuarterlyFinancials) -> list[CalculationResult]:
    """All per-quarter calculations for the focus quarters (oldest → newest)."""
    results: list[CalculationResult] = []
    labels = [p.label for p in fin.quarters]
    s = fin.series

    def at(metric: str, i: int) -> NormalizedValue | None:
        row = s.get(metric) or []
        return row[i] if 0 <= i < len(row) else None

    def prior_index(i: int, years_back: int = 1) -> int | None:
        p = fin.quarters[i]
        target = f"FY{p.fiscal_year - years_back} Q{p.fiscal_quarter}"
        return labels.index(target) if target in labels else None

    start = len(fin.quarters) - fin.focus_quarters
    for i in range(start, len(fin.quarters)):
        rev = operand("revenue", at("revenue", i))
        yi = prior_index(i)
        rev_py = operand("revenue_prior_year", at("revenue", yi)) if yi is not None else None
        rev_pq = operand("revenue_prior_quarter", at("revenue", i - 1)) if i > 0 else None
        results.append(
            growth(
                "revenue_yoy",
                "Revenue growth YoY",
                rev,
                rev_py,
                "(revenue - revenue_prior_year) / |revenue_prior_year|",
                expect="yoy",
            )
        )
        qoq = growth(
            "revenue_qoq",
            "Revenue growth QoQ",
            rev,
            rev_pq,
            "(revenue - revenue_prior_quarter) / |revenue_prior_quarter|",
            expect="qoq",
        )
        qoq.warnings.append("Sequential growth is affected by seasonality.")
        results.append(qoq)

        gp = operand("gross_profit", at("gross_profit", i))
        if gp is not None:
            results.append(ratio("gross_margin", "Gross margin", gp, rev, "gross_profit / revenue"))
        else:
            cor = operand("cost_of_revenue", at("cost_of_revenue", i))
            gp_d = difference(
                "gross_profit_derived",
                "Gross profit (revenue - cost of revenue)",
                rev,
                cor,
                "revenue - cost_of_revenue",
            )
            if gp_d.status == "ok":
                results.append(gp_d)
            gm = ratio(
                "gross_margin",
                "Gross margin",
                operand("gross_profit_derived", gp_d),
                rev,
                "(revenue - cost_of_revenue) / revenue",
            )
            gm.warnings.append(
                "GrossProfit not reported; derived from CostOfRevenue. Filers define "
                "cost of revenue differently, so cross-company comparison is limited."
            )
            if gp_d.status != "ok":
                gm.warnings.extend(gp_d.warnings)
            results.append(gm)

        oi = operand("operating_income", at("operating_income", i))
        ni = operand("net_income", at("net_income", i))
        results.append(ratio("operating_margin", "Operating margin", oi, rev, "operating_income / revenue"))
        results.append(ratio("net_margin", "Net profit margin", ni, rev, "net_income / revenue"))

        ocf = operand("operating_cash_flow", at("operating_cash_flow", i))
        capex = operand("capex", at("capex", i))
        fcf = difference("free_cash_flow", "Free cash flow", ocf, capex, "operating_cash_flow - capex")
        results.append(fcf)
        results.append(
            ratio("fcf_margin", "FCF margin", operand("free_cash_flow", fcf), rev, "free_cash_flow / revenue")
        )
        results.append(ratio("capex_to_revenue", "CAPEX / revenue", capex, rev, "capex / revenue"))
        capex_py = operand("capex_prior_year", at("capex", yi)) if yi is not None else None
        results.append(
            growth(
                "capex_yoy",
                "CAPEX growth YoY",
                capex,
                capex_py,
                "(capex - capex_prior_year) / |capex_prior_year|",
                expect="yoy",
            )
        )
        cov = ratio("ocf_to_capex", "Operating cash flow / CAPEX", ocf, capex, "operating_cash_flow / capex")
        cov.unit = "multiple"
        results.append(cov)

        debt = operand("total_debt", at("total_debt", i))
        cash = operand("cash", at("cash", i))
        nd = difference("net_debt", "Net debt", debt, cash, "total_debt - cash", instant=True)
        nd.warnings.append(
            "Cash excludes marketable securities/short-term investments; net debt "
            "therefore overstates leverage for companies with large investment balances."
        )
        results.append(nd)
    return results


def compute_annual_metrics(fin: QuarterlyFinancials) -> list[CalculationResult]:
    out: list[CalculationResult] = []
    revs = fin.annual.get("revenue", [])
    for prev, cur in itertools.pairwise(revs):
        out.append(
            growth(
                "annual_revenue_yoy",
                "Annual revenue growth",
                operand("revenue", cur),
                operand("revenue_prior_year", prev),
                "(annual_revenue - annual_revenue_prior_year) / |prior|",
                expect="yoy",
            )
        )
    return out
