"""Typed models for event studies (v0.3-B).

A study is fully described by an immutable ``EventStudySpecification``; its canonical JSON hash
(``spec_hash``) plus the digest of the exact market-data versions used (``data_digest``) and the
methodology version determine the run id, so an identical analysis on identical data is
reproducible and never overwritten, while changed data or assumptions produce a new run.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

METHODOLOGY_VERSION = "event-study-v0.3b.1"
MAX_ABS_OFFSET = 500  # sessions; bounds every window definition
MAX_WINDOWS = 8


class ExpectedReturnModel(StrEnum):
    RAW = "raw"  # Model A: observed returns, no benchmark adjustment (descriptive)
    MARKET_ADJUSTED = "market_adjusted"  # Model B: AR = R_i - R_m (no estimation)
    MARKET_MODEL = "market_model"  # Model C: OLS R_i = alpha + beta R_m + e


MODEL_ALIASES = {
    "raw": ExpectedReturnModel.RAW,
    "raw-returns": ExpectedReturnModel.RAW,
    "market-adjusted": ExpectedReturnModel.MARKET_ADJUSTED,
    "market_adjusted": ExpectedReturnModel.MARKET_ADJUSTED,
    "market-model": ExpectedReturnModel.MARKET_MODEL,
    "market_model": ExpectedReturnModel.MARKET_MODEL,
}


def parse_model(name: str) -> ExpectedReturnModel:
    try:
        return MODEL_ALIASES[name.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unknown model {name!r}; use one of raw, market-adjusted, market-model") from exc


class DataMode(StrEnum):
    RESEARCH = "research"  # Mode A: retrospective descriptive research (latest data, all actions)
    POINT_IN_TIME = "point_in_time"  # Mode B: only data and actions known at the decision cutoff


class ReturnType(StrEnum):
    SIMPLE = "simple"
    LOG = "log"


class TimingClass(StrEnum):
    PRE_MARKET = "PRE_MARKET"  # before the regular open (incl. before 04:00 ET)
    REGULAR_SESSION = "REGULAR_SESSION"  # open <= t < close
    AT_CLOSE = "AT_CLOSE"  # exactly the regular close (e.g. 16:00:00 or 13:00:00 early close)
    AFTER_HOURS = "AFTER_HOURS"  # after the close on a session day
    NON_TRADING_DAY = "NON_TRADING_DAY"  # weekend / exchange holiday
    UNCERTAIN_TIME = "UNCERTAIN_TIME"  # only the publication DATE is known
    UNKNOWN = "UNKNOWN"  # no usable publication information -> ineligible


class TimeBasis(StrEnum):
    VERIFIED_TIMESTAMP = "VERIFIED_TIMESTAMP"  # source timestamp (EDGAR acceptance, Fed pubDate)
    OFFICIAL_SCHEDULE_CONFIRMED_DATE = "OFFICIAL_SCHEDULE_CONFIRMED_DATE"  # schedule time, date confirmed
    DATE_ONLY = "DATE_ONLY"
    NONE = "NONE"
    USER_SUPPLIED = "USER_SUPPLIED"  # synthetic/test events constructed by the caller


class StudyStatus(StrEnum):
    COMPLETED = "COMPLETED"
    UNVERIFIED = "UNVERIFIED"  # computed, but point-in-time availability rests on an assumption
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    INELIGIBLE = "INELIGIBLE"  # e.g. unknown publication time, event not known at cutoff
    PARTIAL = "PARTIAL"  # multi-event run stopped by its time budget
    FAILED = "FAILED"


class WindowStatus(StrEnum):
    VALID = "VALID"
    MISSING_OBSERVATIONS = "MISSING_OBSERVATIONS"  # never shortened or filled
    INELIGIBLE_TIMING = "INELIGIBLE_TIMING"  # e.g. [0,0] with an uncertain publication time
    NOT_AVAILABLE_AT_CUTOFF = "NOT_AVAILABLE_AT_CUTOFF"
    OUT_OF_RANGE = "OUT_OF_RANGE"


class InferenceStatus(StrEnum):
    OK = "OK"
    LIMITED = "LIMITED"  # computed, but a diagnostic undermines an assumption
    NOT_APPLICABLE = "NOT_APPLICABLE"  # raw model: descriptive only
    NOT_AVAILABLE = "NOT_AVAILABLE"  # e.g. zero residual variance, invalid window


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def sha256_json(obj: Any) -> str:
    return hashlib.sha256(_canon(obj).encode()).hexdigest()


# ---------------------------------------------------------------------------- windows
class EventWindow(BaseModel):
    """Inclusive trading-session offsets relative to the event session (day 0)."""

    model_config = ConfigDict(frozen=True)
    start: int
    end: int

    @model_validator(mode="after")
    def _check(self) -> EventWindow:
        if self.end < self.start:
            raise ValueError("event window end must not precede start")
        if max(abs(self.start), abs(self.end)) > MAX_ABS_OFFSET:
            raise ValueError(f"window offsets are bounded by +/-{MAX_ABS_OFFSET} sessions")
        return self

    @property
    def label(self) -> str:
        return f"[{self.start:+d},{self.end:+d}]".replace("+0", "0")

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @classmethod
    def parse(cls, text: str) -> EventWindow:
        a, _, b = text.strip().strip("[]").partition(",") if "," in text else text.partition(":")
        return cls(start=int(a), end=int(b))


class EstimationWindow(BaseModel):
    model_config = ConfigDict(frozen=True)
    start: int = -150
    end: int = -21
    min_observations: int = 100

    @model_validator(mode="after")
    def _check(self) -> EstimationWindow:
        if self.end < self.start:
            raise ValueError("estimation window end must not precede start")
        if self.end >= 0:
            raise ValueError("the estimation window must end before the event session")
        if abs(self.start) > MAX_ABS_OFFSET:
            raise ValueError(f"estimation offsets are bounded by {MAX_ABS_OFFSET} sessions")
        if self.min_observations < 10:
            raise ValueError("min_observations must be >= 10")
        if self.min_observations > self.end - self.start + 1:
            raise ValueError("min_observations exceeds the estimation window length")
        return self

    @property
    def length(self) -> int:
        return self.end - self.start + 1


DEFAULT_WINDOWS = (
    EventWindow(start=0, end=0),
    EventWindow(start=0, end=1),
    EventWindow(start=-1, end=1),
    EventWindow(start=0, end=5),
)


class BenchmarkDefinition(BaseModel):
    model_config = ConfigDict(frozen=True)
    instrument_id: str
    description: str = "benchmark ETF total-return series"

    @field_validator("instrument_id")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()


# ---------------------------------------------------------------------------- specification
class EventStudySpecification(BaseModel):
    """Immutable description of ONE analysis. Everything that changes a number is in here."""

    model_config = ConfigDict(frozen=True)

    kind: str = "equity"  # equity | yield
    event_id: str
    event_source: str | None = None
    event_published_at: datetime | None = None  # the time actually used for alignment
    event_time_basis: TimeBasis = TimeBasis.NONE
    event_category_filter: str | None = None  # multi-event selection (recorded per member)
    target: str  # instrument id or yield series id
    benchmark: BenchmarkDefinition | None = None
    price_basis: str | None = "total_return"
    return_type: ReturnType = ReturnType.SIMPLE
    market_data_source: str = "market_price_bars (v0.3-A store)"
    alignment_convention: str = "daily-close-v1"
    estimation_window: EstimationWindow = EstimationWindow()
    event_windows: tuple[EventWindow, ...] = DEFAULT_WINDOWS
    model: ExpectedReturnModel = ExpectedReturnModel.MARKET_MODEL
    test_method: str = "classical-t (MacKinlay 1997) + pre-event placebo"
    data_mode: DataMode = DataMode.RESEARCH
    as_of: datetime | None = None  # decision cutoff (point_in_time only)
    quality_exclusions: tuple[str, ...] = ("PROVISIONAL", "QUARANTINED")
    # sessions removed from the estimation sample (robustness only, e.g. |z| > 4 outliers)
    excluded_estimation_sessions: tuple[date, ...] = ()
    methodology_version: str = METHODOLOGY_VERSION

    @model_validator(mode="after")
    def _check(self) -> EventStudySpecification:
        if len(self.event_windows) == 0 or len(self.event_windows) > MAX_WINDOWS:
            raise ValueError(f"between 1 and {MAX_WINDOWS} event windows are required")
        if self.estimation_window.end >= min(w.start for w in self.event_windows):
            raise ValueError("the estimation window must end before every event window starts")
        if self.data_mode == DataMode.RESEARCH and self.as_of is not None:
            raise ValueError("research mode is retrospective and takes no as_of; use point_in_time")
        if self.data_mode == DataMode.POINT_IN_TIME and self.as_of is None:
            raise ValueError("point_in_time studies require an explicit as_of decision cutoff")
        if self.as_of is not None and self.as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if self.kind == "equity" and self.model != ExpectedReturnModel.RAW and self.benchmark is None:
            raise ValueError("benchmark-adjusted models require a benchmark")
        if self.kind == "equity" and self.price_basis not in ("raw", "split_adjusted", "total_return"):
            raise ValueError("price_basis must be raw, split_adjusted or total_return")
        return self

    def spec_hash(self) -> str:
        return sha256_json(self.model_dump(mode="json"))


# ---------------------------------------------------------------------------- alignment
class EventAlignment(BaseModel):
    event_id: str
    event_time: datetime | None
    event_time_local: str | None
    time_basis: TimeBasis
    timing_class: TimingClass
    local_date: date | None
    day0: date | None  # event (attribution) session
    early_close: bool = False
    contamination_warning: str | None = None  # regular-session releases
    timing_uncertain: bool = False
    eligible: bool = True
    rule: str
    notes: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------- results
class EstimationDiagnostics(BaseModel):
    expected_sessions: int
    valid_observations: int
    missing_sessions: list[dict] = Field(default_factory=list)  # [{session, reason}]
    alpha: float | None = None
    beta: float | None = None
    alpha_se: float | None = None
    beta_se: float | None = None
    residual_variance: float | None = None
    residual_df: int | None = None
    r_squared: float | None = None
    benchmark_variance: float | None = None
    condition_number: float | None = None
    outliers: list[dict] = Field(default_factory=list)
    beta_stability: dict | None = None
    autocorrelation: dict | None = None
    normality: dict | None = None
    event_induced_volatility: dict | None = None
    warnings: list[str] = Field(default_factory=list)
    status: str = "OK"  # OK | UNSTABLE | INSUFFICIENT_DATA


class AbnormalReturnObservation(BaseModel):
    relative_session: int
    session: date
    asset_return: float | None
    benchmark_return: float | None
    expected_return: float | None
    abnormal_return: float | None
    abnormal_return_variance: float | None = None
    note: str | None = None


class Inference(BaseModel):
    status: InferenceStatus
    null_hypothesis: str | None = None
    standard_error: float | None = None
    standard_error_ignoring_estimation_error: float | None = None
    test_statistic: float | None = None
    distribution: str | None = None
    degrees_of_freedom: int | None = None
    p_value: float | None = None
    confidence_level: float = 0.95
    ci_low: float | None = None
    ci_high: float | None = None
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    placebo: dict | None = None


class WindowResult(BaseModel):
    window: str
    start: int
    end: int
    sessions: list[date] = Field(default_factory=list)
    status: WindowStatus
    reason: str | None = None
    raw_cumulative_return: float | None = None  # compounded asset return (descriptive)
    benchmark_cumulative_return: float | None = None
    sum_asset_return: float | None = None
    car: float | None = None  # arithmetic sum of abnormal returns (NOT a compounded return)
    bhar: float | None = None  # buy-and-hold abnormal return vs benchmark (descriptive)
    inference: Inference | None = None


class ConfoundingEvent(BaseModel):
    event_id: str
    event_type: str
    event_subtype: str
    title: str
    published_at: datetime | None
    time_basis: TimeBasis
    affected_entities: list[str]
    relative_session: int | None
    importance_score: float | None
    importance_note: str = "importance is a heuristic ranking score, not a measure of causal impact"
    source_name: str
    source_url: str | None
    reason: str
    relation: str = "confounder"  # confounder | same_announcement | derived


class EventStudyResult(BaseModel):
    """One event x one target x one specification."""

    status: StudyStatus
    status_reason: str | None = None
    spec_hash: str
    alignment: EventAlignment | None = None
    estimation: EstimationDiagnostics | None = None
    observations: list[AbnormalReturnObservation] = Field(default_factory=list)
    windows: list[WindowResult] = Field(default_factory=list)
    confounders: dict | None = None
    data_manifest: dict = Field(default_factory=dict)
    data_origin: str = "unknown"
    synthetic: bool = False
    warnings: list[str] = Field(default_factory=list)
    interpretation_scope: dict = Field(default_factory=dict)


class EventStudyRun(BaseModel):
    run_id: str
    kind: str  # single | yield | compare
    status: StudyStatus
    spec_hash: str
    data_digest: str
    specification: dict
    event_ids: list[str]
    data_origin: str
    synthetic: bool
    created_at: datetime
    methodology_version: str = METHODOLOGY_VERSION
    software: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    report_markdown: str | None = None
    existing: bool = False  # True when an identical run was already stored (not recomputed)


class EventStudyReport(BaseModel):
    run_id: str
    data_origin: str
    synthetic: bool
    banner: str
    sections: dict[str, Any]
    markdown: str


class EventStudyRequest(BaseModel):
    """API/CLI request. Bounded: one target, at most MAX_WINDOWS windows, offsets <= 500."""

    event_id: str
    ticker: str | None = Field(default=None, max_length=12)
    series_id: str | None = Field(default=None, max_length=12)
    benchmark: str | None = Field(default="SPY", max_length=12)
    model: str = "market-model"
    price_basis: str  # explicit: raw | split_adjusted | total_return
    return_type: ReturnType = ReturnType.SIMPLE
    data_mode: DataMode = DataMode.RESEARCH
    as_of: datetime | None = None
    windows: list[str] | None = Field(default=None, max_length=MAX_WINDOWS)
    estimation_start: int = -150
    estimation_end: int = -21
    min_observations: int = 100
    robustness: bool = True

    @model_validator(mode="after")
    def _one_target(self) -> EventStudyRequest:
        if bool(self.ticker) == bool(self.series_id):
            raise ValueError("give exactly one of ticker (equity/ETF study) or series_id (yield study)")
        return self
