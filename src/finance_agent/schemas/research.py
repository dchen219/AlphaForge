"""Research request/response contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from finance_agent.research.trends import Finding, TrendResult
from finance_agent.retrieval.evidence import EvidenceItem
from finance_agent.schemas.financials import (
    CalculationResult,
    FiscalPeriod,
    MissingValue,
    NormalizedValue,
    SnapshotInfo,
)
from finance_agent.verification.checks import VerificationReport

TICKER_PATTERN = r"^[A-Za-z][A-Za-z0-9.\-]{0,9}$"


class ResearchRequest(BaseModel):
    question: str | None = Field(default=None, max_length=2000)
    ticker: str | None = Field(default=None, pattern=TICKER_PATTERN)
    quarters: int = Field(default=8, ge=2, le=20)
    as_of: datetime | None = Field(
        default=None, description="Point-in-time cutoff; only sources available by then are used."
    )
    use_llm: bool = True

    @field_validator("as_of", mode="before")
    @classmethod
    def _parse_as_of(cls, v: object) -> object:
        """Same semantics as the CLI and GET ?as_of=: a bare date means 23:59:59 New York;
        naive datetimes are UTC."""
        from datetime import date as _date

        from finance_agent.timeutil import parse_as_of

        if isinstance(v, str | _date | datetime):
            try:
                return parse_as_of(v)
            except ValueError as exc:
                raise ValueError(f"Invalid as_of {v!r}; use YYYY-MM-DD or ISO 8601") from exc
        return v

    @model_validator(mode="after")
    def _need_subject(self) -> ResearchRequest:
        if not self.question and not self.ticker:
            raise ValueError("Provide a ticker and/or a question.")
        return self

    def effective_question(self) -> str:
        if self.question:
            return self.question
        return (
            f"Analyze {self.ticker}'s revenue growth, operating margins, free cash flow, and "
            f"capital expenditure intensity over the most recent {self.quarters} reported quarters."
        )


class LLMStatus(BaseModel):
    provider: str
    status: str  # generated | unavailable | failed
    notes: list[str] = Field(default_factory=list)


class ResearchReportOut(BaseModel):
    run_id: str
    status: str
    question: str
    company_name: str
    ticker: str
    cik: str
    analysis_timestamp: datetime
    as_of: datetime | None
    availability_policy: str
    data_origin: str
    reporting_periods: list[FiscalPeriod]
    requested_metrics: list[str]
    metric_values: list[NormalizedValue]
    derived_calculations: list[CalculationResult]
    trends: list[TrendResult]
    executive_summary: str
    main_findings: list[Finding]
    model_interpretations: list[Finding]
    model_executive_summary: str | None = None
    source_references: list[EvidenceItem]
    snapshots: list[SnapshotInfo]
    missing_data: list[MissingValue]
    limitations: list[str]
    verification: VerificationReport
    llm: LLMStatus
    markdown: str = ""


class ResearchRunOut(BaseModel):
    run_id: str
    status: str
    question: str
    ticker: str | None
    created_at: datetime
    completed_at: datetime | None
    error: str | None
    steps: list[dict]
    report: ResearchReportOut | None
    as_of: datetime | None = None
    params: dict = Field(default_factory=dict)


class ResearchRunSummary(BaseModel):
    run_id: str
    status: str
    question: str
    ticker: str | None
    as_of: datetime | None
    use_llm: bool | None
    created_at: datetime
    completed_at: datetime | None
    error: str | None
    verification_status: str | None
    steps_executed: int


class ResearchRunList(BaseModel):
    count: int
    runs: list[ResearchRunSummary]
