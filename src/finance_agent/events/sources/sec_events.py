"""SEC corporate events from EDGAR submissions metadata.

Timing: ``acceptanceDateTime`` in data.sec.gov/submissions is true UTC (verified against
the EDGAR SGML header ACCEPTANCE-DATETIME, which is Eastern local time, for both EDT and
EST filings), so filing events carry EXACT_TIMESTAMP availability = acceptance time.

8-K classification uses only the SEC-provided ``items`` metadata (official Form 8-K item
numbers). An 8-K without classifiable items stays a generic FORM_8K filing event.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from finance_agent.errors import MalformedDataError
from finance_agent.events.schemas import (
    AvailabilityPrecision,
    Comparison,
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    RevisionStatus,
    Unit,
    VerificationStatus,
    make_event_id,
)
from finance_agent.events.store import SourceLink
from finance_agent.providers.sec import pad_cik
from finance_agent.schemas.financials import CalculationResult, QuarterlyFinancials
from finance_agent.schemas.sec import accession_nodash, filing_index_url
from finance_agent.storage.snapshots import SnapshotRef
from finance_agent.timeutil import available_at, utcnow

EVENT_FORMS = {"10-K", "10-Q", "8-K", "10-K/A", "10-Q/A", "8-K/A", "10-KT", "10-QT"}

# Official Form 8-K item numbers -> (category, subtype, description). Priority order matters.
ITEM_MAP: list[tuple[str, EventCategory, EventSubtype, str]] = [
    (
        "2.02",
        EventCategory.CORPORATE_EARNINGS,
        EventSubtype.EARNINGS_RELEASE,
        "Results of Operations and Financial Condition",
    ),
    (
        "4.02",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.OTHER_MATERIAL_EVENT,
        "Non-Reliance on Previously Issued Financial Statements",
    ),
    (
        "1.05",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.CYBERSECURITY_INCIDENT,
        "Material Cybersecurity Incidents",
    ),
    (
        "2.01",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.ACQUISITION_DISPOSITION,
        "Completion of Acquisition or Disposition of Assets",
    ),
    (
        "1.01",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.MATERIAL_AGREEMENT,
        "Entry into a Material Definitive Agreement",
    ),
    (
        "1.02",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.MATERIAL_AGREEMENT,
        "Termination of a Material Definitive Agreement",
    ),
    (
        "2.03",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.FINANCING,
        "Creation of a Direct Financial Obligation",
    ),
    (
        "2.04",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.FINANCING,
        "Triggering Events That Accelerate a Financial Obligation",
    ),
    (
        "5.02",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.LEADERSHIP_CHANGE,
        "Departure/Election of Directors or Officers",
    ),
    ("2.06", EventCategory.OTHER_DISCLOSURE, EventSubtype.OTHER_MATERIAL_EVENT, "Material Impairments"),
    (
        "2.05",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.OTHER_MATERIAL_EVENT,
        "Costs Associated with Exit or Disposal Activities",
    ),
    ("1.03", EventCategory.OTHER_DISCLOSURE, EventSubtype.OTHER_MATERIAL_EVENT, "Bankruptcy or Receivership"),
    (
        "5.01",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.OTHER_MATERIAL_EVENT,
        "Changes in Control of Registrant",
    ),
    (
        "4.01",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.OTHER_MATERIAL_EVENT,
        "Changes in Registrant's Certifying Accountant",
    ),
    (
        "3.01",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.OTHER_MATERIAL_EVENT,
        "Notice of Delisting or Listing Rule Failure",
    ),
    (
        "3.02",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.FINANCING,
        "Unregistered Sales of Equity Securities",
    ),
    (
        "3.03",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.GOVERNANCE_CHANGE,
        "Material Modification to Rights of Security Holders",
    ),
    (
        "5.03",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.GOVERNANCE_CHANGE,
        "Amendments to Articles/Bylaws; Change in Fiscal Year",
    ),
    (
        "5.07",
        EventCategory.OTHER_DISCLOSURE,
        EventSubtype.SHAREHOLDER_VOTE,
        "Submission of Matters to a Vote of Security Holders",
    ),
    ("7.01", EventCategory.OTHER_DISCLOSURE, EventSubtype.REG_FD_DISCLOSURE, "Regulation FD Disclosure"),
    ("8.01", EventCategory.OTHER_DISCLOSURE, EventSubtype.OTHER_MATERIAL_EVENT, "Other Events"),
]
ITEM_DESCRIPTIONS = {code: desc for code, _, _, desc in ITEM_MAP} | {
    "9.01": "Financial Statements and Exhibits"
}
EARNINGS_CLUSTER_WINDOW = timedelta(days=3)


@dataclass(frozen=True)
class FilingRecord:
    cik: str
    accession: str
    form: str
    filing_date: date
    report_date: date | None
    accepted_at: datetime | None  # UTC
    items: tuple[str, ...]
    primary_document: str | None
    primary_doc_description: str | None

    @property
    def document_url(self) -> str | None:
        if not self.primary_document:
            return None
        return (
            f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/"
            f"{accession_nodash(self.accession)}/{self.primary_document}"
        )


def _date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10]) if v else None
    except ValueError:
        return None


def parse_acceptance(v: Any) -> datetime | None:
    """'2026-10-28T20:04:44.000Z' -> aware UTC datetime (verified to be UTC)."""
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def parse_submission_filings(payload: Any) -> list[FilingRecord]:
    try:
        cik = pad_cik(payload["cik"])
        r = payload["filings"]["recent"]
        n = len(r["accessionNumber"])
    except (KeyError, TypeError) as exc:
        raise MalformedDataError(f"submissions payload missing field: {exc}") from exc
    for col in ("form", "filingDate", "acceptanceDateTime"):
        if len(r.get(col, [])) != n:
            raise MalformedDataError(f"submissions column {col} length mismatch")

    def col(name: str, i: int) -> Any:
        values = r.get(name)
        return values[i] if values and i < len(values) else None

    out = []
    for i in range(n):
        form = r["form"][i]
        filed = _date(r["filingDate"][i])
        if form not in EVENT_FORMS or filed is None:
            continue
        items = tuple(x.strip() for x in str(col("items", i) or "").split(",") if x.strip())
        out.append(
            FilingRecord(
                cik,
                r["accessionNumber"][i],
                form,
                filed,
                _date(col("reportDate", i)),
                parse_acceptance(r["acceptanceDateTime"][i]),
                items,
                col("primaryDocument", i) or None,
                col("primaryDocDescription", i) or None,
            )
        )
    return sorted(
        out,
        key=lambda f: (
            f.accepted_at or datetime.combine(f.filing_date, datetime.min.time(), UTC),
            f.accession,
        ),
    )


def classify_8k(items: tuple[str, ...]) -> tuple[EventCategory, EventSubtype, list[str]]:
    for code, cat, sub, _ in ITEM_MAP:
        if code in items:
            return cat, sub, [c for c in items if c != code]
    return EventCategory.CORPORATE_FILING, EventSubtype.FORM_8K, list(items)


@dataclass
class SecEventBatch:
    events: list[tuple[FinancialEvent, list[SourceLink]]] = field(default_factory=list)


class SecEventBuilder:
    def __init__(self, ticker: str, company_name: str) -> None:
        self.ticker = ticker
        self.company = company_name

    def filing_events(
        self, filings: list[FilingRecord], ref: SnapshotRef, as_of: datetime | None = None
    ) -> list[tuple[FinancialEvent, list[SourceLink]]]:
        eligible = [
            f for f in filings if as_of is None or (f.accepted_at is not None and f.accepted_at <= as_of)
        ]
        earnings_8k = [f for f in eligible if f.form.startswith("8-K") and "2.02" in f.items]
        out = []
        for f in eligible:
            ev = self._filing_event(f, ref, earnings_8k, eligible)
            link = SourceLink(
                f"sec_submissions:{f.accession}",
                "SEC EDGAR",
                "primary",
                filing_index_url(f.cik, f.accession),
                ref.snapshot_id,
                ref.sha256,
                ref.retrieved_at,
            )
            out.append((ev, [link]))
        return out

    def _cluster_for(self, f: FilingRecord, earnings_8k: list[FilingRecord]) -> tuple[str | None, str | None]:
        """Earnings 8-K (item 2.02) and the 10-Q/10-K filed within 3 days form one economic cluster."""
        anchor = None
        if f.form.startswith("8-K") and "2.02" in f.items:
            anchor = f
        elif f.form in {"10-Q", "10-K", "10-QT", "10-KT"} and f.accepted_at is not None:
            near = [
                e
                for e in earnings_8k
                if e.accepted_at is not None
                and timedelta(0) <= f.accepted_at - e.accepted_at <= EARNINGS_CLUSTER_WINDOW
            ]
            anchor = near[-1] if near else None
        if anchor is None:
            return None, None
        return f"CL-EARN-{f.cik}-{anchor.accession}", make_event_id(f"SEC|{anchor.cik}|{anchor.accession}")

    def _filing_event(
        self,
        f: FilingRecord,
        ref: SnapshotRef,
        earnings_8k: list[FilingRecord],
        all_filings: list[FilingRecord],
    ) -> FinancialEvent:
        supersedes = None
        extra: list[str] = []
        if f.form.startswith("8-K"):
            cat, sub, extra = classify_8k(f.items)
            if f.form.endswith("/A"):
                cat, sub = EventCategory.CORPORATE_FILING, EventSubtype.AMENDMENT
        elif f.form.endswith("/A"):
            cat, sub = EventCategory.CORPORATE_FILING, EventSubtype.AMENDMENT
        else:
            cat = EventCategory.CORPORATE_FILING
            sub = EventSubtype.FORM_10K if f.form.startswith("10-K") else EventSubtype.FORM_10Q
        if f.form.endswith("/A"):
            base = f.form[:-2]
            originals = [
                o
                for o in all_filings
                if o.form == base
                and o.report_date == f.report_date
                and o.accession != f.accession
                and (o.accepted_at or datetime.max.replace(tzinfo=UTC))
                <= (f.accepted_at or datetime.max.replace(tzinfo=UTC))
            ]
            if originals:
                supersedes = make_event_id(f"SEC|{f.cik}|{originals[-1].accession}")
        cluster, primary = self._cluster_for(f, earnings_8k)
        item_text = ", ".join(f"Item {i} {ITEM_DESCRIPTIONS.get(i, 'Unknown item')}" for i in f.items)
        title = f"{self.ticker} {f.form} filed {f.filing_date.isoformat()}"
        if f.form.startswith("8-K") and f.items:
            title += f" — items {', '.join(f.items)}"
        if f.accepted_at is not None:
            avail, precision = f.accepted_at, AvailabilityPrecision.EXACT_TIMESTAMP
        else:
            avail, precision = available_at(f.filing_date), AvailabilityPrecision.DATE_ONLY_CONSERVATIVE
        return FinancialEvent(
            canonical_event_key=f"SEC|{f.cik}|{f.accession}",
            event_type=cat,
            event_subtype=sub,
            event_status=EventStatus.PUBLISHED,
            title=title,
            description=(
                f"{self.company} {f.form}"
                + (f" ({item_text})" if item_text else "")
                + (f"; report period {f.report_date.isoformat()}" if f.report_date else "")
            ),
            source_name="SEC EDGAR",
            source_url=f.document_url or filing_index_url(f.cik, f.accession),
            source_document_id=f.accession,
            source_snapshot_hash=ref.sha256,
            data_origin=ref.origin,
            affected_entities=[self.ticker],
            affected_industries=[],
            occurred_at=f.accepted_at,
            published_at=f.accepted_at,
            available_at=avail,
            availability_precision=precision,
            observation_period=f.report_date.isoformat() if f.report_date else None,
            observation_date=f.report_date,
            revision_status=RevisionStatus.NOT_APPLICABLE,
            verification_status=VerificationStatus.SOURCE_VERIFIED,
            supersedes_event_id=supersedes,
            event_cluster_id=cluster,
            related_event_id=primary
            if primary and primary != make_event_id(f"SEC|{f.cik}|{f.accession}")
            else None,
            ingested_at=utcnow(),
            attributes={
                "cik": f.cik,
                "accession": f.accession,
                "form": f.form,
                "items": list(f.items),
                "secondary_items": extra,
                "filing_date": f.filing_date.isoformat(),
                "index_url": filing_index_url(f.cik, f.accession),
                "primary_document": f.primary_document,
                "primary_doc_description": f.primary_doc_description,
                "classification_basis": "SEC submissions 'items' metadata" if f.items else "form type",
            },
        )

    def metric_change_event(
        self,
        f: FilingRecord,
        fin: QuarterlyFinancials,
        calcs: list[CalculationResult],
        filing_event: FinancialEvent,
    ) -> FinancialEvent | None:
        """What changed in the newly reported quarter (deterministic v0.1 calculations).

        Computed as of the v0.1 conservative availability of the filing's facts, so the
        event is DATE_ONLY_CONSERVATIVE even though the filing itself has an exact time.
        """
        if not fin.quarters or f.report_date is None or fin.quarters[-1].end != f.report_date:
            return None
        latest = fin.quarters[-1].label
        prior = fin.quarters[-2].label if len(fin.quarters) > 1 else None
        by = {(c.name, c.period_label): c for c in calcs if c.status == "ok" and c.value is not None}
        comps: list[Comparison] = []
        for name, label, unit, scale in [
            ("revenue_yoy", "Revenue growth YoY", Unit.PERCENT_CHANGE, 100),
            ("operating_margin", "Operating margin", Unit.PERCENT, 100),
            ("fcf_margin", "FCF margin", Unit.PERCENT, 100),
            ("capex_yoy", "CAPEX growth YoY", Unit.PERCENT_CHANGE, 100),
        ]:
            cur = by.get((name, latest))
            if cur is None:
                continue
            prev = by.get((name, prior)) if prior else None
            comps.append(
                Comparison(
                    comparison_id=f"CMP-{cur.calc_id}",
                    kind=f"metric:{name}",
                    label=f"{label} ({latest})",
                    value=cur.value * scale,
                    unit=unit,
                    formula=cur.formula,
                    inputs=[{"calc_id": cur.calc_id, "period": latest, "value": cur.value * scale}]
                    + (
                        [{"calc_id": prev.calc_id, "period": prior, "value": prev.value * scale}]
                        if prev
                        else []
                    ),
                )
            )
            if prev is not None:
                comps.append(
                    Comparison(
                        comparison_id=f"CMP-D-{cur.calc_id}",
                        kind=f"metric_change:{name}",
                        label=f"Change in {label.lower()} vs {prior}",
                        value=(cur.value - prev.value) * 100,
                        unit=Unit.PERCENTAGE_POINTS,
                        formula=f"({name}[{latest}] - {name}[{prior}]) * 100",
                        inputs=[{"calc_id": cur.calc_id}, {"calc_id": prev.calc_id}],
                    )
                )
        rev = by.get(("revenue_yoy", latest))
        if not comps or rev is None:
            return None
        prev_rev = by.get(("revenue_yoy", prior)) if prior else None
        avail = max(filing_event.available_at, available_at(f.filing_date))
        return FinancialEvent(
            canonical_event_key=f"SEC|{f.cik}|{f.accession}|METRICS",
            event_type=EventCategory.CORPORATE_FILING,
            event_subtype=EventSubtype.FINANCIAL_METRIC_CHANGE,
            event_status=EventStatus.PUBLISHED,
            title=f"{self.ticker} {latest}: reported financial metrics ({f.form})",
            description=f"Deterministic v0.1 calculations for {latest} from XBRL facts in {f.accession}.",
            source_name="SEC EDGAR XBRL",
            source_url=filing_event.source_url,
            source_document_id=f.accession,
            source_snapshot_hash=fin.snapshots[-1].sha256 if fin.snapshots else None,
            data_origin=fin.data_origin,
            affected_entities=[self.ticker],
            available_at=avail,
            availability_precision=AvailabilityPrecision.DATE_ONLY_CONSERVATIVE,
            series_id=f"{self.ticker}:revenue_yoy",
            observation_period=latest,
            observation_date=f.report_date,
            value=rev.value * 100,
            previous_value=prev_rev.value * 100 if prev_rev else None,
            unit=Unit.PERCENT_CHANGE,
            verification_status=VerificationStatus.SOURCE_VERIFIED,
            related_event_id=filing_event.event_id,
            event_cluster_id=filing_event.event_cluster_id,
            comparisons=comps,
            ingested_at=utcnow(),
            attributes={
                "fiscal_period": latest,
                "prior_period": prior,
                "accession": f.accession,
                "as_of_basis": "v0.1 conservative fact availability (filing date + 1 day 00:00 ET)",
            },
        )
