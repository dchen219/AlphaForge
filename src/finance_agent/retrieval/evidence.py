"""Evidence registry: every number in a report resolves to one of these items.

Evidence kinds
- ``fact``         normalized value (V-…) with XBRL source facts
- ``calculation``  deterministic calculation (C-…) with inputs and formula
- ``trend``        trend statistic (T-…) over calculations
- ``filing``       SEC filing (F-<accession>) with official EDGAR URLs
"""

from __future__ import annotations

from pydantic import BaseModel

from finance_agent.research.formatting import fmt_value
from finance_agent.research.trends import TrendResult
from finance_agent.schemas.financials import CalculationResult, QuarterlyFinancials
from finance_agent.schemas.sec import FilingMeta, filing_index_url


class EvidenceItem(BaseModel):
    evidence_id: str
    kind: str
    label: str
    period: str | None = None
    value: float | None = None
    unit: str | None = None
    display: str | None = None
    payload: dict


def build_evidence(
    fin: QuarterlyFinancials,
    calcs: list[CalculationResult],
    trends: list[TrendResult],
    filings: list[FilingMeta],
) -> dict[str, EvidenceItem]:
    items: dict[str, EvidenceItem] = {}
    accessions: set[str] = set()
    for row in fin.series.values():
        for v in row:
            if v is None:
                continue
            items[v.value_id] = EvidenceItem(
                evidence_id=v.value_id,
                kind="fact",
                label=v.label,
                period=v.period.label,
                value=v.value,
                unit=v.unit,
                display=fmt_value(v.value, v.unit),
                payload=v.model_dump(mode="json"),
            )
            accessions.update(s.accession for s in v.sources)
    for vals in fin.annual.values():
        for v in vals:
            items[v.value_id] = EvidenceItem(
                evidence_id=v.value_id,
                kind="fact",
                label=f"{v.label} (annual)",
                period=v.period.label,
                value=v.value,
                unit=v.unit,
                display=fmt_value(v.value, v.unit),
                payload=v.model_dump(mode="json"),
            )
            accessions.update(s.accession for s in v.sources)
    for c in calcs:
        items[c.calc_id] = EvidenceItem(
            evidence_id=c.calc_id,
            kind="calculation",
            label=c.label,
            period=c.period_label,
            value=c.value,
            unit=c.unit,
            display=fmt_value(c.value, c.unit),
            payload=c.model_dump(mode="json"),
        )
    for t in trends:
        items[t.trend_id] = EvidenceItem(
            evidence_id=t.trend_id,
            kind="trend",
            label=f"{t.label} trend",
            period=f"{t.first_period}..{t.last_period}",
            value=t.change,
            unit=t.change_unit,
            display=fmt_value(t.change, t.change_unit),
            payload=t.model_dump(mode="json"),
        )
    by_acc = {f.accession: f for f in filings}
    for acc in sorted(accessions):
        meta = by_acc.get(acc)
        payload = (
            meta.model_dump(mode="json")
            if meta
            else {
                "accession": acc,
                "index_url": filing_index_url(fin.cik, acc),
                "note": "Filing not in submissions 'recent' list; URL constructed from accession.",
            }
        )
        items[f"F-{acc}"] = EvidenceItem(
            evidence_id=f"F-{acc}",
            kind="filing",
            label=f"{meta.form} filed {meta.filing_date}" if meta else f"Filing {acc}",
            payload=payload,
        )
    return items


def numeric_universe(items: dict[str, EvidenceItem], ids: list[str]) -> list[tuple[float, str]]:
    """(value, unit) pairs that a claim citing ``ids`` may legitimately state.

    Includes the cited items, their direct inputs, and trend endpoints/min/max/mean.
    """
    out: list[tuple[float, str]] = []
    for eid in ids:
        it = items.get(eid)
        if it is None:
            continue
        if it.value is not None and it.unit:
            out.append((it.value, it.unit))
        p = it.payload
        if it.kind == "trend":
            for k in ("first_value", "last_value", "min_value", "max_value", "mean_value"):
                out.append((p[k], p["unit"]))
            out.append((p["change"], p["change_unit"]))
            for cid in p.get("input_ids", []):
                out.extend(numeric_universe(items, [cid]))
        elif it.kind == "calculation":
            for inp in p.get("inputs", []):
                out.append((inp["value"], inp["unit"]))
    return out
