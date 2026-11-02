"""Mapping from normalized metrics to candidate US-GAAP XBRL concepts.

Order matters: for each reporting period the first concept with an eligible fact
wins. Selection happens *per period* because filers change tags over time (e.g.
Alphabet moved revenue from RevenueFromContractWithCustomerExcludingAssessedTax to
Revenues in 2025). The concept actually used is recorded on every value.

CAPEX definition (v0.1)
    Cash purchases of property, plant and equipment from the investing section of
    the cash flow statement: ``PaymentsToAcquirePropertyPlantAndEquipment``.
    When a filer does not use that tag we fall back to
    ``PaymentsToAcquireProductiveAssets`` (e.g. Amazon's "Purchases of property and
    equipment"), which may include other productive assets; such values carry a
    warning. EXCLUDED: finance-lease principal repayments, assets acquired under
    finance leases / build-to-suit (non-cash), business acquisitions, intangible
    asset purchases, and proceeds from asset sales (CAPEX is gross, not net).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MetricSpec:
    key: str
    label: str
    concepts: tuple[str, ...]
    kind: str  # "duration" | "instant"
    unit: str = "USD"
    fallback_warnings: dict[str, str] = field(default_factory=dict)


METRICS: dict[str, MetricSpec] = {
    m.key: m
    for m in [
        MetricSpec(
            "revenue",
            "Revenue",
            (
                "RevenueFromContractWithCustomerExcludingAssessedTax",
                "Revenues",
                "RevenueFromContractWithCustomerIncludingAssessedTax",
                "SalesRevenueNet",
            ),
            "duration",
            fallback_warnings={
                "RevenueFromContractWithCustomerIncludingAssessedTax": "Revenue concept includes assessed taxes.",
            },
        ),
        MetricSpec("gross_profit", "Gross profit", ("GrossProfit",), "duration"),
        MetricSpec(
            "cost_of_revenue",
            "Cost of revenue",
            ("CostOfRevenue", "CostOfGoodsAndServicesSold"),
            "duration",
        ),
        MetricSpec("operating_income", "Operating income", ("OperatingIncomeLoss",), "duration"),
        MetricSpec("net_income", "Net income", ("NetIncomeLoss",), "duration"),
        MetricSpec(
            "operating_cash_flow",
            "Cash flow from operations",
            ("NetCashProvidedByUsedInOperatingActivities",),
            "duration",
        ),
        MetricSpec(
            "capex",
            "Capital expenditures",
            ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"),
            "duration",
            fallback_warnings={
                "PaymentsToAcquireProductiveAssets": "CAPEX uses PaymentsToAcquireProductiveAssets (purchases of property and "
                "equipment; may include other productive assets) because "
                "PaymentsToAcquirePropertyPlantAndEquipment is not reported.",
            },
        ),
        MetricSpec(
            "cash",
            "Cash and cash equivalents",
            (
                "CashAndCashEquivalentsAtCarryingValue",
                "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            ),
            "instant",
            fallback_warnings={
                "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents": "Cash figure includes restricted cash.",
            },
        ),
        # Debt components (combined into total_debt by the normalizer). ``DebtCurrent`` is
        # deliberately NOT used: it includes short-term borrowings/commercial paper and
        # would double count with CommercialPaper/ShortTermBorrowings.
        MetricSpec(
            "long_term_debt_total", "Long-term debt incl. current portion", ("LongTermDebt",), "instant"
        ),
        MetricSpec(
            "long_term_debt_noncurrent", "Long-term debt, noncurrent", ("LongTermDebtNoncurrent",), "instant"
        ),
        MetricSpec(
            "long_term_debt_current",
            "Long-term debt, current portion",
            ("LongTermDebtCurrent",),
            "instant",
        ),
        MetricSpec(
            "short_term_borrowings",
            "Short-term borrowings / commercial paper",
            ("CommercialPaper", "ShortTermBorrowings"),
            "instant",
        ),
    ]
}

DEBT_COMPONENTS = (
    "long_term_debt_total",
    "long_term_debt_noncurrent",
    "long_term_debt_current",
    "short_term_borrowings",
)

SUPPORTED_CONCEPTS: frozenset[str] = frozenset(c for m in METRICS.values() for c in m.concepts)
