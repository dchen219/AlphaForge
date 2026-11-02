"""Normalized financial values and calculation results, with provenance."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, Field


class SourceRef(BaseModel):
    """Exact XBRL fact a normalized value was built from."""

    fact_hash: str
    taxonomy: str
    concept: str
    unit: str
    period_start: date | None
    period_end: date
    value: float
    accession: str
    form: str
    filed: date
    available_at: datetime
    filing_url: str
    filer_fiscal_year: int | None = None
    filer_fiscal_period: str | None = None


class FiscalPeriod(BaseModel):
    label: str  # e.g. "FY2026 Q1", "FY2025"
    fiscal_year: int
    fiscal_quarter: int | None  # None for annual
    start: date | None  # None for instants
    end: date
    duration_days: int | None
    period_type: str  # quarter | annual | instant


class AlternativeValue(BaseModel):
    value: float
    accession: str
    form: str
    filed: date


class NormalizedValue(BaseModel):
    value_id: str
    metric: str
    label: str
    value: float
    unit: str
    period: FiscalPeriod
    method: str  # reported | derived_ytd_difference | component_sum
    formula: str | None = None
    concept: str  # concept(s) used, '+'-joined for component sums
    sources: list[SourceRef]
    restated: bool = False  # differing values for this period across filings
    alternative_values: list[AlternativeValue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class MissingValue(BaseModel):
    metric: str
    period_label: str
    reason: str


class SnapshotInfo(BaseModel):
    sha256: str
    url: str
    retrieved_at: datetime
    origin: str


class QuarterlyFinancials(BaseModel):
    cik: str
    ticker: str | None
    company_name: str
    fiscal_year_end_month: int
    as_of: datetime | None
    generated_at: datetime
    availability_policy: str
    selection_policy: str
    quarters: list[FiscalPeriod]  # oldest → newest, includes look-back for YoY
    focus_quarters: int  # the most recent N are in scope
    series: dict[str, list[NormalizedValue | None]]  # aligned with ``quarters``
    annual: dict[str, list[NormalizedValue]] = Field(default_factory=dict)
    missing: list[MissingValue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    data_origin: str = "unknown"
    snapshots: list[SnapshotInfo] = Field(default_factory=list)

    def focus(self) -> list[FiscalPeriod]:
        return self.quarters[-self.focus_quarters :] if self.focus_quarters else []

    def get(self, metric: str, label: str) -> NormalizedValue | None:
        for p, v in zip(self.quarters, self.series.get(metric, []), strict=False):
            if p.label == label:
                return v
        return None


class CalcInput(BaseModel):
    name: str
    value: float
    unit: str
    ref_id: str  # value_id or calc_id
    period_label: str


class CalculationResult(BaseModel):
    calc_id: str
    name: str
    label: str
    period_label: str
    period_start: date | None
    period_end: date
    value: float | None
    unit: str  # ratio | USD | multiple
    formula: str
    inputs: list[CalcInput]
    warnings: list[str] = Field(default_factory=list)
    status: str = "ok"  # ok | unavailable
