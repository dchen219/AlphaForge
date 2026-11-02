"""Fiscal period matching, duplicate/amended selection, standalone-quarter derivation."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from finance_agent.research.concepts import METRICS
from finance_agent.research.normalize import (
    FiscalCalendar,
    Normalizer,
    build_quarterly_financials,
    classify_duration,
    select_by_context,
    snap_month,
    standalone_quarters,
)
from tests.helpers import TEST_CIK, calendar_year_company_facts, fact


def build(facts, fye=12, as_of=None, quarters=4):
    return build_quarterly_financials(
        facts,
        cik=TEST_CIK,
        company_name="SYNTHETIC",
        ticker="TST",
        calendar=FiscalCalendar(fye),
        as_of=as_of,
        quarters=quarters,
        lookback=0,
    )


# ---------------------------------------------------------------- periods


@pytest.mark.parametrize(
    "days,cls",
    [
        (91, "Q"),
        (98, "Q"),
        (182, "H"),
        (273, "9M"),
        (365, "FY"),
        (371, "FY"),
        (60, None),
        (120, None),
        (400, None),
        (None, None),
    ],
)
def test_classify_duration(days, cls):
    assert classify_duration(days) == cls


def test_snap_month_handles_52_53_week_ends():
    assert snap_month(date(2023, 4, 1)) == (2023, 3)
    assert snap_month(date(2024, 12, 28)) == (2024, 12)
    assert snap_month(date(2024, 1, 2)) == (2023, 12)


@pytest.mark.parametrize(
    "fye,end,label",
    [
        (6, date(2025, 9, 30), "FY2026 Q1"),  # Microsoft
        (6, date(2025, 6, 30), "FY2025 Q4"),
        (9, date(2025, 12, 27), "FY2026 Q1"),  # Apple, 52/53-week
        (9, date(2023, 4, 1), "FY2023 Q2"),
        (9, date(2025, 9, 27), "FY2025 Q4"),
        (12, date(2025, 3, 31), "FY2025 Q1"),  # calendar year
        (12, date(2025, 12, 31), "FY2025 Q4"),
    ],
)
def test_fiscal_labels(fye, end, label):
    assert FiscalCalendar(fye).quarter_label(end) == label


def test_calendar_from_mmdd_and_inference():
    assert FiscalCalendar.from_mmdd("0630").fye_month == 6
    assert FiscalCalendar.from_mmdd("0926").fye_month == 9
    assert FiscalCalendar.from_mmdd("0102").fye_month == 12  # 52/53-week year ending early Jan
    assert FiscalCalendar.from_mmdd("13xx") is None and FiscalCalendar.from_mmdd(None) is None
    inferred = FiscalCalendar.infer([fact("Revenues", "2022-07-01", "2023-06-30", 1, form="10-K")])
    assert inferred.fye_month == 6


# ---------------------------------------------------------------- duplicates & amendments


def test_latest_filed_fact_wins_and_restatement_is_flagged():
    orig = fact("Revenues", "2023-01-01", "2023-03-31", 100, accn="0000000001-23-000001", filed="2023-05-01")
    comparative = fact(
        "Revenues", "2023-01-01", "2023-03-31", 100, accn="0000000001-24-000001", filed="2024-05-01"
    )
    sel = select_by_context([orig, comparative])[(date(2023, 1, 1), date(2023, 3, 31))]
    assert sel.fact.accession == "0000000001-24-000001" and not sel.restated

    amended = fact(
        "Revenues",
        "2023-01-01",
        "2023-03-31",
        95,
        accn="0000000001-23-000099",
        form="10-Q/A",
        filed="2023-09-01",
    )
    sel = select_by_context([orig, amended])[(date(2023, 1, 1), date(2023, 3, 31))]
    assert sel.fact.value == 95 and sel.fact.form == "10-Q/A" and sel.restated


def test_amendment_is_ignored_before_it_was_filed():
    orig = fact("Revenues", "2023-01-01", "2023-03-31", 100, accn="0000000001-23-000001", filed="2023-05-01")
    amended = fact(
        "Revenues",
        "2023-01-01",
        "2023-03-31",
        95,
        accn="0000000001-23-000099",
        form="10-Q/A",
        filed="2023-09-01",
    )
    before = build([orig, amended], as_of=datetime(2023, 8, 1, tzinfo=UTC))
    after = build([orig, amended], as_of=datetime(2023, 9, 3, tzinfo=UTC))
    v_before, v_after = before.series["revenue"][-1], after.series["revenue"][-1]
    assert v_before.value == 100 and not v_before.restated and not v_before.alternative_values
    assert v_after.value == 95 and v_after.restated
    assert [a.value for a in v_after.alternative_values] == [100]


# ---------------------------------------------------------------- standalone quarters


def test_q4_derived_from_annual_minus_nine_months_and_q2_q3_from_ytd():
    t = build(calendar_year_company_facts(), as_of=None, quarters=4)
    labels = [p.label for p in t.quarters]
    assert labels == ["FY2023 Q1", "FY2023 Q2", "FY2023 Q3", "FY2023 Q4"]
    rev = t.series["revenue"]
    assert [v.value for v in rev] == [100, 110, 120, 130]  # Q4 = FY 460 - 9M YTD 330
    assert rev[3].method == "derived_ytd_difference"
    assert rev[3].period.start == date(2023, 10, 1) and rev[3].period.end == date(2023, 12, 31)
    ocf = t.series["operating_cash_flow"]
    assert [v.value for v in ocf] == [40, 50, 60, 70]
    assert [v.method for v in ocf] == [
        "reported",
        "derived_ytd_difference",
        "derived_ytd_difference",
        "derived_ytd_difference",
    ]
    assert {s.accession for s in ocf[3].sources} == {"0000000001-24-000010", "0000000001-23-000003"}


def test_q4_revenue_requires_nine_month_ytd_not_sum_of_quarters():
    """Revenue Q4 needs a 9M YTD fact; summing three quarterly facts is NOT a valid derivation."""
    facts = [
        f
        for f in calendar_year_company_facts()
        if f.concept == "Revenues" and classify_duration(f.duration_days) != "9M"
    ]
    t = build(facts, quarters=4)
    # Only Q1..Q3 exist: there is no 9M revenue context, so Q4 cannot be derived.
    assert [p.label for p in t.quarters] == ["FY2023 Q1", "FY2023 Q2", "FY2023 Q3"]
    facts.append(
        fact("Revenues", "2023-01-01", "2023-09-30", 330, accn="0000000001-23-000003", filed="2023-11-01")
    )
    t = build(facts, quarters=4)
    assert t.series["revenue"][-1].value == 130 and t.quarters[-1].label == "FY2023 Q4"


def test_no_derivation_across_different_start_dates():
    """Cumulative periods with different starts (e.g. after a fiscal-year change) are incompatible."""
    facts = [
        fact("NetCashProvidedByUsedInOperatingActivities", "2023-01-01", "2023-06-30", 90),
        fact("NetCashProvidedByUsedInOperatingActivities", "2023-02-01", "2023-03-31", 40),  # 59 days
        fact("NetCashProvidedByUsedInOperatingActivities", "2022-12-01", "2023-03-31", 45),  # 121 days
    ]
    sel = select_by_context(facts)
    assert standalone_quarters(sel) == {}


def test_no_derivation_when_gap_is_not_one_quarter():
    facts = [
        fact("Revenues", "2023-01-01", "2023-12-31", 460, form="10-K"),
        fact("Revenues", "2023-01-01", "2023-06-30", 210),  # 6M, not 9M: FY-6M is not a quarter
    ]
    out = standalone_quarters(select_by_context(facts))
    assert date(2023, 12, 31) not in out


def test_reported_quarter_preferred_and_mismatch_with_ytd_flagged():
    facts = [
        fact("Revenues", "2023-01-01", "2023-03-31", 100),
        fact("Revenues", "2023-04-01", "2023-06-30", 110),
        fact("Revenues", "2023-01-01", "2023-06-30", 215),  # inconsistent YTD
    ]
    out = standalone_quarters(select_by_context(facts))
    q2 = out[date(2023, 6, 30)]
    assert q2.value == 110 and q2.method == "reported"
    assert any("differs from YTD-derived" in w for w in q2.warnings)


def test_unit_mismatch_is_not_mixed():
    facts = [fact("Revenues", "2023-01-01", "2023-03-31", 100, unit="EUR")]
    t = build(facts)
    assert t.quarters == [] and t.warnings


# ---------------------------------------------------------------- concepts & missing data


def test_concept_fallback_is_per_period_and_recorded():
    facts = [
        fact(
            "RevenueFromContractWithCustomerExcludingAssessedTax", "2024-10-01", "2024-12-31", 90, form="10-K"
        ),
        fact("Revenues", "2025-01-01", "2025-03-31", 95),
    ]
    t = build(facts, quarters=2)
    assert [v.concept for v in t.series["revenue"]] == [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
    ]


def test_missing_concepts_are_reported_not_fabricated():
    t = build([fact("Revenues", "2024-01-01", "2024-03-31", 100)], quarters=1)
    assert t.series["gross_profit"] == [None] and t.series["capex"] == [None]
    missing = {m.metric for m in t.missing}
    assert {"gross_profit", "operating_income", "capex", "cash", "total_debt"} <= missing


def test_capex_fallback_concept_carries_warning():
    facts = [
        fact("Revenues", "2024-01-01", "2024-03-31", 100),
        fact("PaymentsToAcquireProductiveAssets", "2024-01-01", "2024-03-31", 20),
    ]
    v = build(facts, quarters=1).series["capex"][0]
    assert v.concept == "PaymentsToAcquireProductiveAssets"
    assert any("PaymentsToAcquireProductiveAssets" in w for w in v.warnings)


def test_quarterly_gap_is_warned():
    facts = [
        fact("Revenues", "2024-01-01", "2024-03-31", 100),
        fact("Revenues", "2024-07-01", "2024-09-30", 100),
    ]
    assert any("Gap" in w for w in build(facts).warnings)


# ---------------------------------------------------------------- debt


def _debt(facts, end="2024-03-31"):
    norm = Normalizer(facts, FiscalCalendar(12), None)
    keys = (
        "long_term_debt_total",
        "long_term_debt_noncurrent",
        "long_term_debt_current",
        "short_term_borrowings",
    )
    parts = {k: norm.instant(METRICS[k]) for k in keys}
    return norm.total_debt(date.fromisoformat(end), parts)


def test_total_debt_does_not_double_count_current_portion():
    v = _debt(
        [
            fact("LongTermDebt", None, "2024-03-31", 100),
            fact("LongTermDebtNoncurrent", None, "2024-03-31", 90),
            fact("LongTermDebtCurrent", None, "2024-03-31", 10),
            fact("CommercialPaper", None, "2024-03-31", 5),
        ]
    )
    assert v.value == 105 and v.formula == "LongTermDebtNoncurrent + LongTermDebtCurrent + CommercialPaper"
    assert "LongTermDebt" not in v.concept.split("+")  # total never added to its components


def test_total_debt_from_components_and_warning_when_current_missing():
    v = _debt(
        [
            fact("LongTermDebtNoncurrent", None, "2024-03-31", 90),
            fact("LongTermDebtCurrent", None, "2024-03-31", 10),
        ]
    )
    assert v.value == 100 and v.method == "component_sum" and len(v.sources) == 2
    v = _debt([fact("LongTermDebtNoncurrent", None, "2024-03-31", 90)])
    assert v.value == 90 and any("Current portion" in w for w in v.warnings)
    assert _debt([fact("CommercialPaper", None, "2024-03-31", 5)]) is None


def test_instant_values_match_quarter_end_exactly():
    facts = [
        fact("Revenues", "2024-01-01", "2024-03-31", 100),
        fact("CashAndCashEquivalentsAtCarryingValue", None, "2024-03-30", 50),
    ]
    t = build(facts, quarters=1)
    assert t.series["cash"] == [None]


def test_debt_current_is_not_combined_with_commercial_paper():
    """Regression: DebtCurrent includes commercial paper, so it must not be added to it."""
    v = _debt(
        [
            fact("LongTermDebtNoncurrent", None, "2024-03-31", 90),
            fact("DebtCurrent", None, "2024-03-31", 15),
            fact("CommercialPaper", None, "2024-03-31", 5),
        ]
    )
    assert v.value == 95 and "DebtCurrent" not in v.concept
