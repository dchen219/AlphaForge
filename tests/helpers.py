"""Builders for synthetic (clearly fake) SEC/XBRL test data."""

from __future__ import annotations

from datetime import date
from typing import Any

from finance_agent.schemas.sec import XbrlFact

TEST_CIK = "0000000001"


def fact(
    concept: str,
    start: str | None,
    end: str,
    value: float,
    *,
    accn: str = "0000000001-24-000001",
    form: str = "10-Q",
    filed: str = "2024-05-01",
    unit: str = "USD",
    fy: int | None = None,
    fp: str | None = None,
) -> XbrlFact:
    return XbrlFact(
        cik=TEST_CIK,
        taxonomy="us-gaap",
        concept=concept,
        unit=unit,
        start=date.fromisoformat(start) if start else None,
        end=date.fromisoformat(end),
        value=value,
        accession=accn,
        form=form,
        filed=date.fromisoformat(filed),
        fy=fy,
        fp=fp,
    )


def companyfacts_payload(
    rows: dict[str, list[dict[str, Any]]], cik: int = 1, name: str = "SYNTHETIC TEST CO"
) -> dict:
    return {
        "cik": cik,
        "entityName": name,
        "facts": {"us-gaap": {c: {"label": c, "units": {"USD": r}} for c, r in rows.items()}},
    }


def calendar_year_company_facts() -> list[XbrlFact]:
    """A fake December-FYE filer with 10-Q YTD cash flows and a 10-K (all values invented).

    FY2023: Q1..Q3 revenue reported quarterly plus 9M YTD; Q4 only via FY total.
    Cash flow statement reported YTD only (3M, 6M, 9M, 12M).
    """
    f: list[XbrlFact] = []
    q = [
        ("2023-01-01", "2023-03-31", "2023-05-01", "A1"),
        ("2023-04-01", "2023-06-30", "2023-08-01", "A2"),
        ("2023-07-01", "2023-09-30", "2023-11-01", "A3"),
    ]
    revs = [100.0, 110.0, 120.0]
    for (s, e, filed, a), r in zip(q, revs, strict=True):
        f.append(fact("Revenues", s, e, r, accn=f"0000000001-23-00000{a[1]}", filed=filed))
    # The Q3 10-Q also reports 9M YTD revenue (needed for a valid Q4 derivation).
    f.append(
        fact("Revenues", "2023-01-01", "2023-09-30", 330.0, accn="0000000001-23-000003", filed="2023-11-01")
    )
    f.append(
        fact(
            "Revenues",
            "2023-01-01",
            "2023-12-31",
            460.0,
            accn="0000000001-24-000010",
            form="10-K",
            filed="2024-02-15",
        )
    )
    # YTD operating cash flow: 3M=40, 6M=90, 9M=150, FY=220 -> quarters 40, 50, 60, 70
    ytd = [
        ("2023-03-31", 40.0, "2023-05-01", "1"),
        ("2023-06-30", 90.0, "2023-08-01", "2"),
        ("2023-09-30", 150.0, "2023-11-01", "3"),
    ]
    for e, v, filed, n in ytd:
        f.append(
            fact(
                "NetCashProvidedByUsedInOperatingActivities",
                "2023-01-01",
                e,
                v,
                accn=f"0000000001-23-00000{n}",
                filed=filed,
            )
        )
    f.append(
        fact(
            "NetCashProvidedByUsedInOperatingActivities",
            "2023-01-01",
            "2023-12-31",
            220.0,
            accn="0000000001-24-000010",
            form="10-K",
            filed="2024-02-15",
        )
    )
    return f
