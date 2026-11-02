"""v0.3-A market-data models (provider-independent).

Raw provider observations are stored separately from normalized bars; every normalized bar
keeps its provider, provider symbol, source snapshot, retrieval time, availability time and
quality status. Several versions of the same provider observation may exist (corrections);
nothing is silently overwritten.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


class AssetClass(StrEnum):
    EQUITY = "EQUITY"
    ETF = "ETF"


class AdjustmentBasis(StrEnum):
    """Price basis. Stored bars are RAW (or a provider-adjusted basis, labelled by provider);
    SPLIT_ADJUSTED / TOTAL_RETURN are derived internally from stored corporate actions."""

    RAW = "raw"
    SPLIT_ADJUSTED = "split_adjusted"
    TOTAL_RETURN = "total_return"  # split + cash-dividend reinvestment factors (CRSP-style)
    PROVIDER_SPLIT = "provider:split"  # provider's own split adjustment (reconciliation only)
    PROVIDER_ALL = "provider:all"  # provider's own split+dividend adjustment (reconciliation only)


class BarStatus(StrEnum):
    FINAL = "FINAL"
    PROVISIONAL = "PROVISIONAL"  # current/unfinished session; never presented as final


class QualityStatus(StrEnum):
    OK = "OK"
    WARNING = "WARNING"  # flagged, kept in results (e.g. extreme but plausible move)
    QUARANTINED = "QUARANTINED"  # invalid; excluded from queries and calculations


class AvailabilityPrecision(StrEnum):
    EXACT_TIMESTAMP = "EXACT_TIMESTAMP"
    DATE_ONLY_CONSERVATIVE = "DATE_ONLY_CONSERVATIVE"  # next day 00:00 America/New_York
    RULE_BASED_CONSERVATIVE = "RULE_BASED_CONSERVATIVE"  # documented publication rule
    OBSERVED_AT_RETRIEVAL = "OBSERVED_AT_RETRIEVAL"  # correction: known from when we saw it


class CorporateActionType(StrEnum):
    STOCK_SPLIT = "STOCK_SPLIT"  # forward or reverse (ratio new/old)
    CASH_DIVIDEND = "CASH_DIVIDEND"
    SYMBOL_CHANGE = "SYMBOL_CHANGE"
    MERGER = "MERGER"
    SPINOFF = "SPINOFF"


class SessionPhase(StrEnum):
    PRE_MARKET = "PRE_MARKET"
    REGULAR = "REGULAR"
    AFTER_HOURS = "AFTER_HOURS"
    CLOSED = "CLOSED"  # trading day, outside extended hours (overnight)
    NON_TRADING_DAY = "NON_TRADING_DAY"


class QualityCheck(StrEnum):
    DUPLICATE_BAR = "DUPLICATE_BAR"
    MISSING_FIELD = "MISSING_FIELD"
    NONPOSITIVE_PRICE = "NONPOSITIVE_PRICE"
    NEGATIVE_VOLUME = "NEGATIVE_VOLUME"
    HIGH_BELOW_LOW = "HIGH_BELOW_LOW"
    OHLC_INCONSISTENT = "OHLC_INCONSISTENT"
    NON_SESSION_BAR = "NON_SESSION_BAR"  # bar dated on a weekend/holiday
    WRONG_TIMEZONE = "WRONG_TIMEZONE"  # timestamp does not map to its session date
    MISSING_SESSION = "MISSING_SESSION"
    DUPLICATE_PROVIDER_SYMBOL = "DUPLICATE_PROVIDER_SYMBOL"
    EXTREME_RETURN = "EXTREME_RETURN"
    CORPORATE_ACTION_DISCONTINUITY = "CORPORATE_ACTION_DISCONTINUITY"  # explained by a split
    UNEXPLAINED_DISCONTINUITY = "UNEXPLAINED_DISCONTINUITY"  # split-like jump, no recorded action
    INCONSISTENT_ADJUSTMENT = "INCONSISTENT_ADJUSTMENT"
    PROVISIONAL_BAR = "PROVISIONAL_BAR"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    INVALID_CORPORATE_ACTION = "INVALID_CORPORATE_ACTION"
    CORRECTION = "CORRECTION"  # provider changed a previously stored observation


class GapKind(StrEnum):
    """Why a session has no usable bar — never filled with a nearby price."""

    MARKET_CLOSED = "MARKET_CLOSED"
    MISSING_DATA = "MISSING_DATA"  # provider answered; no bar (a full-day halt is indistinguishable)
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    NOT_FINALIZED = "NOT_FINALIZED"
    QUARANTINED = "QUARANTINED"
    TRADING_HALT = "TRADING_HALT"  # only when halt data is supplied (none in v0.3-A)


# --------------------------------------------------------------------------- models


class Instrument(BaseModel):
    instrument_id: str = Field(pattern=r"^[A-Z0-9.\-]{1,16}$")  # internal id (= canonical ticker)
    name: str
    asset_class: AssetClass
    group: str  # e.g. broad_equity, treasury_etf, sector_etf, technology
    currency: str = "USD"
    primary_exchange: str | None = None  # MIC when known
    calendar: str = "XNYS"
    active: bool = True


class ProviderInstrumentMapping(BaseModel):
    instrument_id: str
    provider: str
    provider_symbol: str
    valid_from: date | None = None  # symbol changes: mapping validity window
    valid_to: date | None = None


class PriceBar(BaseModel):
    instrument_id: str
    provider: str
    provider_symbol: str
    exchange: str | None = None
    session_date: date
    bar_timestamp: datetime  # provider timestamp (UTC), as reported
    timezone: str = "America/New_York"  # session timezone
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    vwap: float | None = None
    trade_count: int | None = None
    adjustment: AdjustmentBasis = AdjustmentBasis.RAW
    currency: str = "USD"
    frequency: str = "1D"
    snapshot_id: int | None = None
    snapshot_sha256: str | None = None
    data_origin: str = "live"
    retrieved_at: datetime
    available_at: datetime
    availability_precision: AvailabilityPrecision = AvailabilityPrecision.DATE_ONLY_CONSERVATIVE
    bar_status: BarStatus = BarStatus.FINAL
    quality_status: QualityStatus = QualityStatus.OK
    version: int = 1

    @field_validator("bar_timestamp", "retrieved_at", "available_at")
    @classmethod
    def _aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware")
        return v

    def values(self) -> tuple:
        return (self.open, self.high, self.low, self.close, self.volume, self.vwap, self.trade_count)


class CorporateAction(BaseModel):
    action_id: str  # provider id, or deterministic hash when absent
    provider: str
    instrument_id: str
    action_type: CorporateActionType
    ex_date: date | None = None  # mergers: effective date
    record_date: date | None = None
    payable_date: date | None = None
    process_date: date | None = None
    declaration_date: date | None = None  # rarely provided; None = unknown (never invented)
    old_rate: float | None = None
    new_rate: float | None = None
    cash_amount: float | None = None
    currency: str = "USD"
    new_symbol: str | None = None
    snapshot_id: int | None = None
    snapshot_sha256: str | None = None
    data_origin: str = "live"
    retrieved_at: datetime
    available_at: datetime
    availability_precision: AvailabilityPrecision = AvailabilityPrecision.DATE_ONLY_CONSERVATIVE
    raw: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate(self) -> CorporateAction:
        problems = self.problems()
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def problems(self) -> list[str]:
        out = []
        if self.action_type == CorporateActionType.STOCK_SPLIT:
            if not (self.old_rate and self.new_rate) or self.old_rate <= 0 or self.new_rate <= 0:
                out.append("split requires positive old_rate and new_rate")
            if self.ex_date is None:
                out.append("split requires ex_date")
        if self.action_type == CorporateActionType.CASH_DIVIDEND:
            if self.cash_amount is None or self.cash_amount < 0:
                out.append("cash dividend requires a non-negative cash_amount")
            if self.ex_date is None:
                out.append("cash dividend requires ex_date")
        return out

    @property
    def split_ratio(self) -> float | None:
        """Shares after / shares before (10-for-1 forward split = 10; 1-for-10 reverse = 0.1)."""
        if self.action_type != CorporateActionType.STOCK_SPLIT or not self.old_rate:
            return None
        return (self.new_rate or 0) / self.old_rate


class AdjustmentFactor(BaseModel):
    """Multiplicative factor applied to prices on sessions *before* ``ex_date``.

    Derived (not provider-supplied): the source corporate action and, for dividends, the raw
    close used are recorded so the adjustment can be reproduced.
    """

    instrument_id: str
    ex_date: date
    basis: AdjustmentBasis
    price_factor: float
    volume_factor: float = 1.0
    source_action_id: str
    source_provider: str
    method: str  # e.g. "split: old/new", "dividend: 1 - D / close(prev session)"
    reference_close: float | None = None
    reference_session: date | None = None


class TradingSession(BaseModel):
    calendar: str
    session_date: date
    market_open: datetime  # UTC instants from the exchange calendar
    market_close: datetime
    pre_market_open: datetime | None = None
    after_hours_close: datetime | None = None
    early_close: bool = False


class MarketDataSnapshot(BaseModel):
    snapshot_id: int
    sha256: str
    provider: str
    url: str
    retrieved_at: datetime
    origin: str


class DataQualityIssue(BaseModel):
    issue_id: int | None = None
    check: QualityCheck
    severity: str  # INFO | WARNING | ERROR
    instrument_id: str | None = None
    provider: str | None = None
    session_date: date | None = None
    detail: str
    status: str = "OPEN"  # OPEN | QUARANTINED | EXPLAINED
    run_id: str | None = None
    detected_at: datetime | None = None


class MarketDataIngestionRun(BaseModel):
    run_id: str
    provider: str
    instruments: list[str]
    start: date
    end: date
    status: str  # running | completed | completed_with_errors | failed
    started_at: datetime
    finished_at: datetime | None = None
    requests: int = 0
    cache_hits: int = 0
    rows_received: int = 0
    versions_inserted: int = 0
    unchanged: int = 0
    duplicates: int = 0
    corrections: int = 0
    corporate_actions: int = 0
    quality_issues: int = 0
    errors: list[str] = Field(default_factory=list)
    data_origin: str | None = None
    duration_seconds: float | None = None
