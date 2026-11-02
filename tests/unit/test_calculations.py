"""Financial ratio arithmetic against hand-calculated expected values."""

from __future__ import annotations

from datetime import date

import pytest

from finance_agent.research.calculations import (
    Operand,
    compute_annual_metrics,
    compute_quarterly_metrics,
    difference,
    growth,
    ratio,
)
from finance_agent.research.normalize import FiscalCalendar, build_quarterly_financials
from tests.helpers import TEST_CIK, fact


def op(name, value, start="2024-01-01", end="2024-03-31", unit="USD", fy=2024, fq=1, ref="V-x"):
    return Operand(
        name,
        value,
        unit,
        ref,
        f"FY{fy} Q{fq}",
        date.fromisoformat(start) if start else None,
        date.fromisoformat(end),
        fy,
        fq,
    )


def test_ratio_basic_and_provenance():
    r = ratio(
        "operating_margin", "Operating margin", op("oi", 30, ref="V-a"), op("rev", 120, ref="V-b"), "oi / rev"
    )
    assert r.value == pytest.approx(0.25) and r.status == "ok" and r.unit == "ratio"
    assert [i.ref_id for i in r.inputs] == ["V-a", "V-b"] and r.formula == "oi / rev"


def test_ratio_division_by_zero_and_missing_inputs():
    r = ratio("m", "m", op("a", 1), op("b", 0), "a/b")
    assert r.value is None and r.status == "unavailable" and any("zero" in w for w in r.warnings)
    r = ratio("m", "m", op("a", 1), None, "a/b")
    assert r.value is None and any("Missing input" in w for w in r.warnings)


def test_ratio_rejects_unit_and_period_mismatch():
    assert ratio("m", "m", op("a", 1), op("b", 2, unit="EUR"), "a/b").value is None
    r = ratio("m", "m", op("a", 1), op("b", 2, start="2023-10-01", end="2023-12-31"), "a/b")
    assert r.value is None and any("Period mismatch" in w for w in r.warnings)


def test_growth_yoy_and_qoq():
    cur = op("rev", 125, fy=2024, fq=1)
    prior = op("rev_py", 100, start="2023-01-01", end="2023-03-31", fy=2023, fq=1)
    assert growth("g", "g", cur, prior, "f", expect="yoy").value == pytest.approx(0.25)
    pq = op("rev_pq", 120, start="2023-10-01", end="2023-12-31", fy=2023, fq=4)
    assert growth("g", "g", cur, pq, "f", expect="qoq").value == pytest.approx(5 / 120)


def test_growth_rejects_non_comparable_periods():
    cur = op("rev", 125, fy=2024, fq=1)
    wrong_q = op("x", 100, start="2023-04-01", end="2023-06-30", fy=2023, fq=2)
    r = growth("g", "g", cur, wrong_q, "f", expect="yoy")
    assert r.value is None and any("not the expected YOY" in w for w in r.warnings)
    half = op("x", 100, start="2023-01-01", end="2023-06-30", fy=2023, fq=1)
    assert growth("g", "g", cur, half, "f", expect="yoy").value is None


def test_growth_zero_and_negative_base():
    cur = op("ni", 10)
    assert (
        growth(
            "g", "g", cur, op("p", 0, start="2023-01-01", end="2023-03-31", fy=2023), "f", expect="yoy"
        ).value
        is None
    )
    r = growth("g", "g", cur, op("p", -20, start="2023-01-01", end="2023-03-31", fy=2023), "f", expect="yoy")
    assert r.value == pytest.approx(1.5) and any("negative" in w for w in r.warnings)  # (10 - -20)/20


def test_difference_instant_matches_on_end_date():
    a = op("debt", 70, start=None)
    b = op("cash", 20, start=None)
    assert difference("nd", "nd", a, b, "debt - cash", instant=True).value == 50
    c = op("cash", 20, start=None, end="2024-03-30")
    assert difference("nd", "nd", a, c, "debt - cash", instant=True).value is None


def _synthetic_two_years():
    """Fake filer: FY2023 Q1 and FY2024 Q1, hand-picked round numbers."""
    f = []
    for yr, rev, gp, oi, ni, ocf, capex, cash, debt in [
        (2023, 80, 48, 20, 15, 40, 16, 25, 75),
        (2024, 100, 62, 30, 24, 50, 20, 20, 70),
    ]:
        s, e, filed = f"{yr}-01-01", f"{yr}-03-31", f"{yr}-05-01"
        a = f"0000000001-{yr % 100}-000001"
        for concept, v in [
            ("Revenues", rev),
            ("GrossProfit", gp),
            ("OperatingIncomeLoss", oi),
            ("NetIncomeLoss", ni),
            ("NetCashProvidedByUsedInOperatingActivities", ocf),
            ("PaymentsToAcquirePropertyPlantAndEquipment", capex),
        ]:
            f.append(fact(concept, s, e, v, accn=a, filed=filed))
        f.append(fact("CashAndCashEquivalentsAtCarryingValue", None, e, cash, accn=a, filed=filed))
        f.append(fact("LongTermDebt", None, e, debt, accn=a, filed=filed))
    # 2023 Q2..Q4 so the sequence has no gaps
    for s, e in [("2023-04-01", "2023-06-30"), ("2023-07-01", "2023-09-30")]:
        f.append(fact("Revenues", s, e, 90, filed="2023-11-01"))
    f.append(fact("Revenues", "2023-01-01", "2023-09-30", 260, filed="2023-11-01"))
    f.append(fact("Revenues", "2023-01-01", "2023-12-31", 350, form="10-K", filed="2024-02-01"))
    return f


def test_compute_quarterly_metrics_hand_calculated():
    fin = build_quarterly_financials(
        _synthetic_two_years(),
        cik=TEST_CIK,
        company_name="SYN",
        ticker="SYN",
        calendar=FiscalCalendar(12),
        as_of=None,
        quarters=1,
        lookback=4,
    )
    calcs = {c.name: c for c in compute_quarterly_metrics(fin)}
    expected = {
        "revenue_yoy": 0.25,  # (100-80)/80
        "revenue_qoq": (100 - 90) / 90,  # Q4'23 = 350 - 260 = 90
        "gross_margin": 0.62,  # 62/100
        "operating_margin": 0.30,
        "net_margin": 0.24,
        "free_cash_flow": 30.0,  # 50-20
        "fcf_margin": 0.30,
        "capex_to_revenue": 0.20,
        "capex_yoy": 0.25,  # (20-16)/16
        "ocf_to_capex": 2.5,
        "net_debt": 50.0,  # 70-20
    }
    for name, value in expected.items():
        assert calcs[name].status == "ok", (name, calcs[name].warnings)
        assert calcs[name].value == pytest.approx(value), name
    assert calcs["ocf_to_capex"].unit == "multiple"
    assert calcs["fcf_margin"].inputs[0].ref_id == calcs["free_cash_flow"].calc_id


def test_gross_margin_falls_back_to_cost_of_revenue_with_warning():
    f = [
        fact("Revenues", "2024-01-01", "2024-03-31", 200),
        fact("CostOfRevenue", "2024-01-01", "2024-03-31", 150),
    ]
    fin = build_quarterly_financials(
        f, cik=TEST_CIK, company_name="S", ticker="S", calendar=FiscalCalendar(12), as_of=None, quarters=1
    )
    calcs = {c.name: c for c in compute_quarterly_metrics(fin)}
    assert calcs["gross_profit_derived"].value == 50
    assert calcs["gross_margin"].value == pytest.approx(0.25)
    assert any("GrossProfit not reported" in w for w in calcs["gross_margin"].warnings)
    assert calcs["capex_to_revenue"].status == "unavailable"  # no CAPEX facts: not forced


def test_annual_revenue_growth():
    f = [
        fact("Revenues", "2022-01-01", "2022-12-31", 400, form="10-K", filed="2023-02-01"),
        fact("Revenues", "2023-01-01", "2023-12-31", 500, form="10-K", filed="2024-02-01"),
        fact("Revenues", "2024-01-01", "2024-03-31", 130),
    ]
    fin = build_quarterly_financials(
        f, cik=TEST_CIK, company_name="S", ticker="S", calendar=FiscalCalendar(12), as_of=None, quarters=1
    )
    (g,) = compute_annual_metrics(fin)
    assert g.value == pytest.approx(0.25) and g.period_label == "FY2023"
