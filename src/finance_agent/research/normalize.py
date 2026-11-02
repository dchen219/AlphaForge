"""Period-aware normalization of XBRL facts into fiscal-quarter values.

Key rules (see docs/accounting_methodology.md):
1. Only facts from periodic reports (10-K/10-Q and amendments) whose filing was
   available at ``as_of`` (conservative policy) are eligible.
2. Facts are grouped by exact reporting context (concept, unit, start, end). When
   several filings report the same context (comparatives, amendments,
   restatements) the most recently *filed* eligible fact wins ("latest known as
   of as_of"); differing values are flagged as restatements.
3. Each period is classified by duration (quarter / 6M / 9M / annual YTD).
   ``fy``/``fp`` in companyfacts describe the filing, not the fact, so fiscal
   labels are derived from period end dates and the fiscal-year-end month.
4. Standalone quarters are taken directly when reported; otherwise derived as
   ``cumulative(A) − cumulative(B)`` only when A and B share the same start date
   (same fiscal year), B is exactly one quarter shorter, and B ends ~one quarter
   before A. Anything else is not derived.
5. Instant (balance-sheet) values are matched to quarter-end dates exactly.
"""

from __future__ import annotations

import hashlib
import itertools
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from finance_agent.research.concepts import METRICS, MetricSpec
from finance_agent.schemas.financials import (
    AlternativeValue,
    FiscalPeriod,
    MissingValue,
    NormalizedValue,
    QuarterlyFinancials,
    SourceRef,
)
from finance_agent.schemas.sec import XbrlFact, filing_index_url
from finance_agent.timeutil import AVAILABILITY_POLICY, is_available, utcnow

SELECTION_POLICY = "latest_filed_eligible_fact_per_reporting_context"

# Inclusive day counts (end - start + 1).
DURATION_CLASSES = {
    "Q": (84, 100),
    "H": (175, 190),
    "9M": (266, 282),
    "FY": (357, 374),
}
CUMULATIVE_PREDECESSOR = {"H": "Q", "9M": "H", "FY": "9M"}
RESTATEMENT_TOLERANCE = 0.5  # USD; XBRL values are reported in whole units


def classify_duration(days: int | None) -> str | None:
    if days is None:
        return None
    for name, (lo, hi) in DURATION_CLASSES.items():
        if lo <= days <= hi:
            return name
    return None


def snap_month(d: date) -> tuple[int, int]:
    """Map a (possibly 52/53-week) period end to its nominal (year, month)."""
    if d.day >= 15:
        return d.year, d.month
    return (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)


@dataclass(frozen=True)
class FiscalCalendar:
    fye_month: int  # 1..12

    @classmethod
    def from_mmdd(cls, mmdd: str | None) -> FiscalCalendar | None:
        if not mmdd or len(mmdd) != 4 or not mmdd.isdigit():
            return None
        month, day = int(mmdd[:2]), int(mmdd[2:])
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
        # Same snapping as period ends: a year ending in the first days of a month
        # (52/53-week calendars) belongs to the previous month.
        return cls(fye_month=month if day >= 15 else (12 if month == 1 else month - 1))

    @classmethod
    def infer(cls, facts: Iterable[XbrlFact]) -> FiscalCalendar:
        counts: dict[int, int] = defaultdict(int)
        for f in facts:
            if classify_duration(f.duration_days) == "FY":
                counts[snap_month(f.end)[1]] += 1
        return cls(max(counts, key=counts.get) if counts else 12)

    def fiscal_year(self, end: date) -> int:
        y, m = snap_month(end)
        return y if m <= self.fye_month else y + 1

    def fiscal_quarter(self, end: date) -> int:
        _, m = snap_month(end)
        return ((m - self.fye_month - 1) % 12) // 3 + 1

    def quarter_label(self, end: date) -> str:
        return f"FY{self.fiscal_year(end)} Q{self.fiscal_quarter(end)}"


@dataclass
class Selected:
    fact: XbrlFact
    candidates: list[XbrlFact]

    @property
    def restated(self) -> bool:
        vals = {round(c.value, 2) for c in self.candidates}
        return max(vals) - min(vals) > RESTATEMENT_TOLERANCE if len(vals) > 1 else False


def eligible(facts: Iterable[XbrlFact], as_of: datetime | None, unit: str) -> list[XbrlFact]:
    return [f for f in facts if f.unit == unit and is_available(f.filed, as_of)]


def select_by_context(facts: Iterable[XbrlFact]) -> dict[tuple[date | None, date], Selected]:
    groups: dict[tuple[date | None, date], list[XbrlFact]] = defaultdict(list)
    for f in facts:
        groups[(f.start, f.end)].append(f)
    out = {}
    for key, cands in groups.items():
        cands.sort(key=lambda f: (f.filed, f.accession))
        out[key] = Selected(fact=cands[-1], candidates=cands)
    return out


def _source(f: XbrlFact) -> SourceRef:
    return SourceRef(
        fact_hash=f.fact_hash,
        taxonomy=f.taxonomy,
        concept=f.concept,
        unit=f.unit,
        period_start=f.start,
        period_end=f.end,
        value=f.value,
        accession=f.accession,
        form=f.form,
        filed=f.filed,
        available_at=f.available_at,
        filing_url=filing_index_url(f.cik, f.accession),
        filer_fiscal_year=f.fy,
        filer_fiscal_period=f.fp,
    )


def _alternatives(sel: Selected) -> list[AlternativeValue]:
    return [
        AlternativeValue(value=c.value, accession=c.accession, form=c.form, filed=c.filed)
        for c in sel.candidates
        if c is not sel.fact and abs(c.value - sel.fact.value) > RESTATEMENT_TOLERANCE
    ]


def _value_id(metric: str, end: date, start: date | None, sources: list[SourceRef], method: str) -> str:
    key = f"{metric}|{start}|{end}|{method}|" + ",".join(sorted(s.fact_hash for s in sources))
    return "V-" + hashlib.sha256(key.encode()).hexdigest()[:12]


@dataclass
class QuarterCandidate:
    start: date
    end: date
    value: float
    method: str
    sources: list[SourceRef]
    restated: bool
    alternatives: list[AlternativeValue]
    formula: str | None = None
    warnings: list[str] = field(default_factory=list)


def standalone_quarters(selected: dict[tuple[date | None, date], Selected]) -> dict[date, QuarterCandidate]:
    """Direct quarterly facts plus continuity-checked YTD differences, keyed by end date."""
    by_class: dict[str, list[Selected]] = defaultdict(list)
    for (start, _end), sel in selected.items():
        if start is None:
            continue
        cls = classify_duration(sel.fact.duration_days)
        if cls:
            by_class[cls].append(sel)

    out: dict[date, QuarterCandidate] = {}
    for sel in by_class["Q"]:
        f = sel.fact
        assert f.start is not None
        out[f.end] = QuarterCandidate(
            start=f.start,
            end=f.end,
            value=f.value,
            method="reported",
            sources=[_source(f)],
            restated=sel.restated,
            alternatives=_alternatives(sel),
        )

    for cls, prev_cls in CUMULATIVE_PREDECESSOR.items():
        prev_index = {(s.fact.start, s.fact.end): s for s in by_class[prev_cls]}
        for a in by_class[cls]:
            fa = a.fact
            assert fa.start is not None
            # B must share A's start (same fiscal-year YTD) and end ~one quarter earlier.
            matches = [
                b
                for (bstart, bend), b in prev_index.items()
                if bstart == fa.start and 80 <= (fa.end - bend).days <= 100
            ]
            if len(matches) != 1:
                continue
            b = matches[0]
            fb = b.fact
            q_start = fb.end + timedelta(days=1)
            derived = QuarterCandidate(
                start=q_start,
                end=fa.end,
                value=fa.value - fb.value,
                method="derived_ytd_difference",
                formula=f"{fa.concept}[{fa.start}..{fa.end}] - {fb.concept}[{fb.start}..{fb.end}]",
                sources=[_source(fa), _source(fb)],
                restated=a.restated or b.restated,
                alternatives=[],
                warnings=[
                    f"Standalone quarter derived from cumulative periods "
                    f"({fa.duration_days}d minus {fb.duration_days}d, same start {fa.start})."
                ],
            )
            existing = out.get(fa.end)
            if existing is None:
                out[fa.end] = derived
            elif abs(existing.value - derived.value) > max(1.0, abs(existing.value) * 1e-6):
                existing.warnings.append(
                    f"Reported quarterly value differs from YTD-derived value "
                    f"({derived.value:,.0f}); reported value used."
                )
    return out


class Normalizer:
    def __init__(self, facts: list[XbrlFact], calendar: FiscalCalendar, as_of: datetime | None) -> None:
        self.facts_by_concept: dict[str, list[XbrlFact]] = defaultdict(list)
        for f in facts:
            self.facts_by_concept[f.concept].append(f)
        self.calendar = calendar
        self.as_of = as_of

    def _period(self, start: date | None, end: date, kind: str) -> FiscalPeriod:
        fy = self.calendar.fiscal_year(end)
        if kind == "annual":
            return FiscalPeriod(
                label=f"FY{fy}",
                fiscal_year=fy,
                fiscal_quarter=None,
                start=start,
                end=end,
                duration_days=(end - start).days + 1 if start else None,
                period_type="annual",
            )
        fq = self.calendar.fiscal_quarter(end)
        return FiscalPeriod(
            label=f"FY{fy} Q{fq}",
            fiscal_year=fy,
            fiscal_quarter=fq,
            start=start,
            end=end,
            duration_days=(end - start).days + 1 if start else None,
            period_type="instant" if kind == "instant" else "quarter",
        )

    def _concept_warning(self, spec: MetricSpec, concept: str) -> list[str]:
        w = spec.fallback_warnings.get(concept)
        return [w] if w else []

    def quarterly(self, spec: MetricSpec) -> dict[date, NormalizedValue]:
        """End date → standalone quarterly value (first concept in priority order wins)."""
        merged: dict[date, NormalizedValue] = {}
        for concept in spec.concepts:
            facts = eligible(self.facts_by_concept.get(concept, []), self.as_of, spec.unit)
            if not facts:
                continue
            for end, qc in standalone_quarters(select_by_context(facts)).items():
                if end in merged:
                    continue
                merged[end] = NormalizedValue(
                    value_id=_value_id(spec.key, end, qc.start, qc.sources, qc.method),
                    metric=spec.key,
                    label=spec.label,
                    value=qc.value,
                    unit=spec.unit,
                    period=self._period(qc.start, end, "quarter"),
                    method=qc.method,
                    formula=qc.formula,
                    concept=concept,
                    sources=qc.sources,
                    restated=qc.restated,
                    alternative_values=qc.alternatives,
                    warnings=qc.warnings + self._concept_warning(spec, concept),
                )
        return merged

    def annual(self, spec: MetricSpec) -> dict[date, NormalizedValue]:
        merged: dict[date, NormalizedValue] = {}
        for concept in spec.concepts:
            facts = eligible(self.facts_by_concept.get(concept, []), self.as_of, spec.unit)
            for (start, end), sel in select_by_context(facts).items():
                if start is None or end in merged or classify_duration(sel.fact.duration_days) != "FY":
                    continue
                src = [_source(sel.fact)]
                merged[end] = NormalizedValue(
                    value_id=_value_id(spec.key + ":annual", end, start, src, "reported"),
                    metric=spec.key,
                    label=spec.label,
                    value=sel.fact.value,
                    unit=spec.unit,
                    period=self._period(start, end, "annual"),
                    method="reported",
                    concept=concept,
                    sources=src,
                    restated=sel.restated,
                    alternative_values=_alternatives(sel),
                    warnings=self._concept_warning(spec, concept),
                )
        return merged

    def instant(self, spec: MetricSpec) -> dict[date, NormalizedValue]:
        merged: dict[date, NormalizedValue] = {}
        for concept in spec.concepts:
            facts = [
                f
                for f in eligible(self.facts_by_concept.get(concept, []), self.as_of, spec.unit)
                if f.is_instant
            ]
            for (_s, end), sel in select_by_context(facts).items():
                if end in merged:
                    continue
                src = [_source(sel.fact)]
                merged[end] = NormalizedValue(
                    value_id=_value_id(spec.key, end, None, src, "reported"),
                    metric=spec.key,
                    label=spec.label,
                    value=sel.fact.value,
                    unit=spec.unit,
                    period=self._period(None, end, "instant"),
                    method="reported",
                    concept=concept,
                    sources=src,
                    restated=sel.restated,
                    alternative_values=_alternatives(sel),
                    warnings=self._concept_warning(spec, concept),
                )
        return merged

    def total_debt(self, end: date, parts: dict[str, dict[date, NormalizedValue]]) -> NormalizedValue | None:
        """Total debt = long-term debt incl. current portion + short-term borrowings.

        Preference (consistent measurement basis first):
        1. LongTermDebtNoncurrent + LongTermDebtCurrent — balance-sheet carrying amounts;
        2. LongTermDebt — includes the current portion but filers often tag it at
           face/principal value in the debt footnote, so it is used only when the
           components are not both available (flagged);
        3. LongTermDebtNoncurrent alone (current portion untagged; flagged).
        LongTermDebt is never added to its own components. Leases are excluded.
        """
        total = parts["long_term_debt_total"].get(end)
        noncur = parts["long_term_debt_noncurrent"].get(end)
        cur = parts["long_term_debt_current"].get(end)
        short = parts["short_term_borrowings"].get(end)
        warnings: list[str] = []
        used: list[NormalizedValue] = []
        if noncur is not None and cur is not None:
            used += [noncur, cur]
            formula = f"LongTermDebtNoncurrent + {cur.concept}"
        elif total is not None:
            used.append(total)
            formula = "LongTermDebt"
            warnings.append(
                "Balance-sheet debt components unavailable for this date; used LongTermDebt, which "
                "some filers report at face/principal value rather than carrying amount."
            )
        elif noncur is not None:
            used.append(noncur)
            formula = "LongTermDebtNoncurrent"
            warnings.append(
                "Current portion of long-term debt not reported for this date; treated as not present."
            )
        else:
            return None
        if short is not None:
            used.append(short)
            formula += f" + {short.concept}"
        else:
            warnings.append("No short-term borrowings/commercial paper fact for this date.")
        sources = [s for v in used for s in v.sources]
        return NormalizedValue(
            value_id=_value_id("total_debt", end, None, sources, "component_sum"),
            metric="total_debt",
            label="Total debt (excl. leases)",
            value=sum(v.value for v in used),
            unit="USD",
            period=self._period(None, end, "instant"),
            method="component_sum",
            formula=formula,
            concept="+".join(v.concept for v in used),
            sources=sources,
            restated=any(v.restated for v in used),
            warnings=warnings,
        )


QUARTERLY_METRICS = (
    "revenue",
    "gross_profit",
    "cost_of_revenue",
    "operating_income",
    "net_income",
    "operating_cash_flow",
    "capex",
)
INSTANT_METRICS = ("cash",)
ANNUAL_METRICS = ("revenue", "operating_income", "net_income", "operating_cash_flow", "capex")


def build_quarterly_financials(
    facts: list[XbrlFact],
    *,
    cik: str,
    company_name: str,
    ticker: str | None,
    calendar: FiscalCalendar,
    as_of: datetime | None,
    quarters: int = 8,
    lookback: int = 4,
    anchor_metric: str = "revenue",
) -> QuarterlyFinancials:
    norm = Normalizer(facts, calendar, as_of)
    q_values = {m: norm.quarterly(METRICS[m]) for m in QUARTERLY_METRICS}
    i_values = {m: norm.instant(METRICS[m]) for m in INSTANT_METRICS}
    debt_parts = {
        m: norm.instant(METRICS[m])
        for m in (
            "long_term_debt_total",
            "long_term_debt_noncurrent",
            "long_term_debt_current",
            "short_term_borrowings",
        )
    }

    anchor = q_values[anchor_metric]
    warnings: list[str] = []
    ends = sorted(anchor)
    if not ends:
        warnings.append(
            f"No eligible quarterly {anchor_metric} facts available"
            + (f" as of {as_of.isoformat()}" if as_of else "")
            + "."
        )
    selected_ends = ends[-(quarters + lookback) :]
    periods = [anchor[e].period for e in selected_ends]

    # Detect gaps in the fiscal-quarter sequence.
    for prev, cur in itertools.pairwise(periods):
        expected = (
            (prev.fiscal_year, prev.fiscal_quarter + 1)
            if prev.fiscal_quarter < 4
            else (prev.fiscal_year + 1, 1)
        )
        if (cur.fiscal_year, cur.fiscal_quarter) != expected:
            warnings.append(f"Gap in quarterly sequence between {prev.label} and {cur.label}.")

    series: dict[str, list[NormalizedValue | None]] = {}
    missing: list[MissingValue] = []
    focus_labels = {p.label for p in periods[-quarters:]}

    def record_missing(metric: str, p: FiscalPeriod, reason: str) -> None:
        if p.label in focus_labels:
            missing.append(MissingValue(metric=metric, period_label=p.label, reason=reason))

    for m, values in q_values.items():
        row: list[NormalizedValue | None] = []
        for p in periods:
            v = values.get(p.end)
            if v is not None and v.period.start != p.start:
                record_missing(
                    m,
                    p,
                    f"Reported period {v.period.start}..{v.period.end} does not "
                    f"match revenue period {p.start}..{p.end}.",
                )
                v = None
            elif v is None:
                concepts = ", ".join(METRICS[m].concepts)
                record_missing(
                    m,
                    p,
                    f"No eligible standalone-quarter fact or valid YTD derivation for concepts: {concepts}.",
                )
            row.append(v)
        series[m] = row
    for m, values in i_values.items():
        row = []
        for p in periods:
            v = values.get(p.end)
            if v is None:
                record_missing(m, p, "No instant fact at quarter-end date.")
            row.append(v)
        series[m] = row
    debt_row: list[NormalizedValue | None] = []
    for p in periods:
        v = norm.total_debt(p.end, debt_parts)
        if v is None:
            record_missing("total_debt", p, "No long-term debt facts at quarter-end date.")
        debt_row.append(v)
    series["total_debt"] = debt_row

    annual = {}
    for m in ANNUAL_METRICS:
        vals = norm.annual(METRICS[m])
        annual[m] = [vals[e] for e in sorted(vals)][-3:]

    if any(v and v.restated for row in series.values() for v in row):
        warnings.append(
            "Some values were reported differently across filings (restated/recast); "
            "the latest eligible filing's value is used and alternatives are listed."
        )
    return QuarterlyFinancials(
        cik=cik,
        ticker=ticker,
        company_name=company_name,
        fiscal_year_end_month=calendar.fye_month,
        as_of=as_of,
        generated_at=utcnow(),
        availability_policy=AVAILABILITY_POLICY,
        selection_policy=SELECTION_POLICY,
        quarters=periods,
        focus_quarters=min(quarters, len(periods)),
        series=series,
        annual=annual,
        missing=missing,
        warnings=warnings,
    )
