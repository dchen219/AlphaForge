"""Evidence-to-claim references, numeric claim checking, arithmetic re-verification."""

from __future__ import annotations

import pytest

from finance_agent.research.calculations import compute_quarterly_metrics
from finance_agent.research.normalize import FiscalCalendar, build_quarterly_financials
from finance_agent.research.trends import Finding, compute_trends, template_findings
from finance_agent.retrieval.evidence import build_evidence, numeric_universe
from finance_agent.verification.checks import (
    check_arithmetic,
    check_numeric_claim,
    check_numeric_claims,
    check_provenance,
    check_references,
    extract_numbers,
)
from tests.helpers import TEST_CIK, fact


def test_extract_numbers_ignores_labels_ids_and_dates():
    text = (
        "In FY2026 Q1 (see T-0b165be11dd3, 0000950170-24-008814, 2024-01-30) revenue was $92.28B, up 18.8%."
    )
    nums = extract_numbers(text)
    assert [(n, s) for n, _, s, _ in nums] == [(92.28, "b"), (18.8, "%")]


@pytest.mark.parametrize(
    "text,ok",
    [
        ("Operating margin was 49.8%.", True),
        ("Operating margin was 49.79%.", True),  # more precise but consistent
        ("Operating margin was 51.0%.", False),  # fabricated
        ("Revenue reached $92.28B.", True),
        ("Revenue reached $92,280 million.", True),
        ("Revenue reached $95.0B.", False),
        ("Margin expanded by +4.3 pp over 8 quarters.", True),
        ("Margin expanded by 7.0 pp.", False),
        ("Coverage was 1.47x.", True),
    ],
)
def test_numeric_claim_check(text, ok):
    universe = [(0.4979, "ratio"), (92.28e9, "USD"), (0.043, "ratio_change"), (1.47, "multiple")]
    assert (check_numeric_claim(text, universe) == []) is ok


def _evidence():
    facts = []
    for yr, rev, oi in [(2023, 80, 20), (2024, 100, 30)]:
        for q, (s, e) in enumerate([("01-01", "03-31"), ("04-01", "06-30"), ("07-01", "09-30")], 1):
            a = f"0000000001-{yr % 100}-00000{q}"
            facts.append(fact("Revenues", f"{yr}-{s}", f"{yr}-{e}", rev + q, accn=a, filed=f"{yr}-11-0{q}"))
            facts.append(
                fact("OperatingIncomeLoss", f"{yr}-{s}", f"{yr}-{e}", oi + q, accn=a, filed=f"{yr}-11-0{q}")
            )
    fin = build_quarterly_financials(
        facts,
        cik=TEST_CIK,
        company_name="SYN",
        ticker="SYN",
        calendar=FiscalCalendar(12),
        as_of=None,
        quarters=3,
        lookback=4,
    )
    calcs = compute_quarterly_metrics(fin)
    trends = compute_trends(calcs)
    return fin, calcs, trends, build_evidence(fin, calcs, trends, [])


def test_template_findings_are_fully_supported():
    fin, _calcs, trends, items = _evidence()
    findings = template_findings(trends, fin)
    assert findings
    assert check_references(findings, items).status == "passed"
    assert check_numeric_claims(findings, items).status == "passed"


def test_unknown_or_missing_references_fail():
    _, _, _, items = _evidence()
    bad = [
        Finding(finding_id="X1", section="growth", text="Revenue grew.", evidence_ids=["C-doesnotexist"]),
        Finding(finding_id="X2", section="growth", text="Revenue grew.", evidence_ids=[]),
    ]
    res = check_references(bad, items)
    assert res.status == "failed" and len(res.details) == 2


def test_fabricated_number_with_valid_reference_is_caught():
    _, _, trends, items = _evidence()
    t = trends[0]
    f = Finding(
        finding_id="X",
        section="growth",
        text="Revenue growth reached 99.9% in FY2024 Q3.",
        evidence_ids=[t.trend_id],
    )
    assert check_numeric_claims([f], items).status == "failed"


def test_numeric_universe_includes_trend_inputs():
    _, _, trends, items = _evidence()
    universe = numeric_universe(items, [trends[0].trend_id])
    assert len(universe) > 5


def test_arithmetic_check_detects_tampering():
    _, calcs, _, items = _evidence()
    assert check_arithmetic(items).status == "passed"
    victim = next(c for c in calcs if c.status == "ok" and c.name == "operating_margin")
    items[victim.calc_id].payload["value"] = victim.value + 0.01
    res = check_arithmetic(items)
    assert res.status == "failed" and victim.calc_id in res.details[0]


def test_arithmetic_check_detects_tampered_fact_and_provenance_gap():
    fin, _, _, items = _evidence()
    v = fin.series["revenue"][-1]
    items[v.value_id].payload["value"] = v.value + 1
    assert check_arithmetic(items).status == "failed"
    items[v.value_id].payload["sources"][0]["accession"] = ""
    assert check_provenance(items).status == "failed"
