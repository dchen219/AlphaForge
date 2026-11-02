"""Unified financial event schema (v0.2).

Classification uses controlled enums. Subtypes are validated against their category
(``SUBTYPES_BY_CATEGORY``); extend the enums and that mapping together.

Fields are optional when not applicable — never populated with invented values.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class EventCategory(StrEnum):
    MACRO_RELEASE = "MACRO_RELEASE"
    CENTRAL_BANK = "CENTRAL_BANK"
    CORPORATE_FILING = "CORPORATE_FILING"
    CORPORATE_EARNINGS = "CORPORATE_EARNINGS"
    CORPORATE_GUIDANCE = "CORPORATE_GUIDANCE"  # reserved: no verified source in v0.2
    OTHER_DISCLOSURE = "OTHER_DISCLOSURE"


class EventSubtype(StrEnum):
    # macro indicators
    CPI = "CPI"
    CORE_CPI = "CORE_CPI"
    PCE_PRICE_INDEX = "PCE_PRICE_INDEX"
    CORE_PCE_PRICE_INDEX = "CORE_PCE_PRICE_INDEX"
    NONFARM_PAYROLLS = "NONFARM_PAYROLLS"
    UNEMPLOYMENT_RATE = "UNEMPLOYMENT_RATE"
    REAL_GDP = "REAL_GDP"
    RELEASE_SCHEDULED = "RELEASE_SCHEDULED"  # calendar entry for a multi-indicator release
    # central bank
    RATE_HIKE = "RATE_HIKE"
    RATE_CUT = "RATE_CUT"
    RATE_HOLD = "RATE_HOLD"
    POLICY_STATEMENT = "POLICY_STATEMENT"  # statement whose decision could not be classified
    MEETING_SCHEDULED = "MEETING_SCHEDULED"
    # corporate filings
    FORM_10K = "FORM_10K"
    FORM_10Q = "FORM_10Q"
    FORM_8K = "FORM_8K"
    AMENDMENT = "AMENDMENT"
    FINANCIAL_METRIC_CHANGE = "FINANCIAL_METRIC_CHANGE"
    # 8-K item based
    EARNINGS_RELEASE = "EARNINGS_RELEASE"
    LEADERSHIP_CHANGE = "LEADERSHIP_CHANGE"
    MATERIAL_AGREEMENT = "MATERIAL_AGREEMENT"
    FINANCING = "FINANCING"
    ACQUISITION_DISPOSITION = "ACQUISITION_DISPOSITION"
    REG_FD_DISCLOSURE = "REG_FD_DISCLOSURE"
    SHAREHOLDER_VOTE = "SHAREHOLDER_VOTE"
    CYBERSECURITY_INCIDENT = "CYBERSECURITY_INCIDENT"
    GOVERNANCE_CHANGE = "GOVERNANCE_CHANGE"
    OTHER_MATERIAL_EVENT = "OTHER_MATERIAL_EVENT"
    GUIDANCE_UPDATE = "GUIDANCE_UPDATE"  # reserved


SUBTYPES_BY_CATEGORY: dict[EventCategory, frozenset[EventSubtype]] = {
    EventCategory.MACRO_RELEASE: frozenset(
        {
            EventSubtype.CPI,
            EventSubtype.CORE_CPI,
            EventSubtype.PCE_PRICE_INDEX,
            EventSubtype.CORE_PCE_PRICE_INDEX,
            EventSubtype.NONFARM_PAYROLLS,
            EventSubtype.UNEMPLOYMENT_RATE,
            EventSubtype.REAL_GDP,
            EventSubtype.RELEASE_SCHEDULED,
        }
    ),
    EventCategory.CENTRAL_BANK: frozenset(
        {
            EventSubtype.RATE_HIKE,
            EventSubtype.RATE_CUT,
            EventSubtype.RATE_HOLD,
            EventSubtype.POLICY_STATEMENT,
            EventSubtype.MEETING_SCHEDULED,
        }
    ),
    EventCategory.CORPORATE_FILING: frozenset(
        {
            EventSubtype.FORM_10K,
            EventSubtype.FORM_10Q,
            EventSubtype.FORM_8K,
            EventSubtype.AMENDMENT,
            EventSubtype.FINANCIAL_METRIC_CHANGE,
        }
    ),
    EventCategory.CORPORATE_EARNINGS: frozenset({EventSubtype.EARNINGS_RELEASE}),
    EventCategory.CORPORATE_GUIDANCE: frozenset({EventSubtype.GUIDANCE_UPDATE}),
    EventCategory.OTHER_DISCLOSURE: frozenset(
        {
            EventSubtype.LEADERSHIP_CHANGE,
            EventSubtype.MATERIAL_AGREEMENT,
            EventSubtype.FINANCING,
            EventSubtype.ACQUISITION_DISPOSITION,
            EventSubtype.REG_FD_DISCLOSURE,
            EventSubtype.SHAREHOLDER_VOTE,
            EventSubtype.CYBERSECURITY_INCIDENT,
            EventSubtype.GOVERNANCE_CHANGE,
            EventSubtype.OTHER_MATERIAL_EVENT,
        }
    ),
}


class EventStatus(StrEnum):
    SCHEDULED = "SCHEDULED"  # calendar entry only — NOT evidence that it occurred
    PUBLISHED = "PUBLISHED"
    REVISED = "REVISED"  # a later vintage changed a previously published value
    CANCELLED = "CANCELLED"
    POSTPONED = "POSTPONED"


class RevisionStatus(StrEnum):
    FIRST_RELEASE = "FIRST_RELEASE"
    REVISION = "REVISION"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class AvailabilityPrecision(StrEnum):
    EXACT_TIMESTAMP = "EXACT_TIMESTAMP"  # e.g. EDGAR acceptance time, Fed RSS pubDate
    # Publication date confirmed by a source vintage; time = official scheduled time.
    SCHEDULED_TIME_CONFIRMED_DATE = "SCHEDULED_TIME_CONFIRMED_DATE"
    DATE_ONLY_CONSERVATIVE = "DATE_ONLY_CONSERVATIVE"  # next day 00:00 New York
    UNKNOWN = "UNKNOWN"  # not usable for historical replay


class VerificationStatus(StrEnum):
    SOURCE_VERIFIED = "SOURCE_VERIFIED"  # occurrence and values confirmed by an official source
    SCHEDULE_ONLY = "SCHEDULE_ONLY"  # only a calendar entry exists
    UNCONFIRMED = "UNCONFIRMED"  # expected publication not confirmed by source data
    UNVERIFIED = "UNVERIFIED"


class ImportanceLevel(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ConsensusStatus(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class Unit(StrEnum):
    INDEX_POINTS = "INDEX_POINTS"
    PERCENT = "PERCENT"  # a rate level, e.g. unemployment 4.1 %
    PERCENT_CHANGE = "PERCENT_CHANGE"  # growth, e.g. +0.3 % m/m
    PERCENTAGE_POINTS = "PERCENTAGE_POINTS"  # difference between two rates
    THOUSANDS_OF_PERSONS = "THOUSANDS_OF_PERSONS"
    BILLIONS_USD = "BILLIONS_USD"
    USD = "USD"
    BASIS_POINTS = "BASIS_POINTS"
    COUNT = "COUNT"


class Comparison(BaseModel):
    """A deterministic quantitative comparison with its formula and inputs."""

    comparison_id: str
    kind: str  # change_from_previous | pct_change_from_previous | yoy_pct_change | yoy_change | revision | percentile | standardized_change | surprise | rate_change
    label: str
    value: float | None
    unit: Unit
    formula: str
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    status: str = "ok"  # ok | unavailable
    note: str | None = None


class EconomicChannel(BaseModel):
    trigger_event_id: str
    source_variable: str
    target_variable: str
    direction: str  # up | down | ambiguous | not_determined
    mechanism: str
    supporting_evidence: list[str]
    interpretation_type: (
        str  # ACCOUNTING_IDENTITY | ECONOMIC_HYPOTHESIS | HISTORICAL_STATISTICAL | OBSERVED_MARKET_REACTION
    )
    confidence_basis: str
    limitations: list[str] = Field(default_factory=list)


def make_event_id(canonical_event_key: str) -> str:
    """Deterministic id: the same economic event always gets the same id."""
    return "EV-" + hashlib.sha256(canonical_event_key.encode()).hexdigest()[:16]


class FinancialEvent(BaseModel):
    event_id: str = ""
    canonical_event_key: str
    event_type: EventCategory
    event_subtype: EventSubtype
    event_status: EventStatus
    title: str
    description: str | None = None

    source_name: str
    source_url: str | None = None
    source_document_id: str | None = None  # e.g. SEC accession, Fed release URL id, vintage id
    source_snapshot_hash: str | None = None
    data_origin: str = "unknown"

    affected_entities: list[str] = Field(default_factory=list)  # tickers, series ids
    affected_industries: list[str] = Field(default_factory=list)

    occurred_at: datetime | None = None  # e.g. FOMC decision time, filing event date
    scheduled_for: datetime | None = None
    published_at: datetime | None = None  # verified publication instant
    available_at: datetime | None = None  # earliest instant usable in historical replay
    ingested_at: datetime | None = None
    availability_precision: AvailabilityPrecision = AvailabilityPrecision.UNKNOWN

    series_id: str | None = None
    observation_period: str | None = None  # e.g. "2026-09", "2026-Q3"
    observation_date: date | None = None
    value: float | None = None
    previous_value: float | None = None
    consensus_value: float | None = None
    consensus_status: ConsensusStatus = ConsensusStatus.UNAVAILABLE
    unit: Unit | None = None
    revision_status: RevisionStatus = RevisionStatus.NOT_APPLICABLE

    importance_score: float | None = None
    importance_level: ImportanceLevel | None = None
    importance_components: dict[str, Any] = Field(default_factory=dict)
    verification_status: VerificationStatus = VerificationStatus.UNVERIFIED

    related_event_id: str | None = None
    supersedes_event_id: str | None = None
    event_cluster_id: str | None = None

    comparisons: list[Comparison] = Field(default_factory=list)
    channels: list[EconomicChannel] = Field(default_factory=list)
    attributes: dict[str, Any] = Field(default_factory=dict)

    created_at: datetime | None = None
    updated_at: datetime | None = None

    @field_validator(
        "occurred_at",
        "scheduled_for",
        "published_at",
        "available_at",
        "ingested_at",
        "created_at",
        "updated_at",
    )
    @classmethod
    def _tz_aware(cls, v: datetime | None) -> datetime | None:
        if v is not None and v.tzinfo is None:
            raise ValueError("event timestamps must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _consistency(self) -> FinancialEvent:
        if self.event_subtype not in SUBTYPES_BY_CATEGORY[self.event_type]:
            raise ValueError(f"subtype {self.event_subtype} is not valid for category {self.event_type}")
        # A calendar entry is not evidence of occurrence: it carries no observed values.
        if self.event_status == EventStatus.SCHEDULED and (
            self.published_at is not None or self.value is not None
        ):
            raise ValueError("SCHEDULED events cannot carry publication times or observed values")
        if self.consensus_status == ConsensusStatus.UNAVAILABLE and self.consensus_value is not None:
            raise ValueError("consensus_value requires consensus_status=available")
        if not self.event_id:
            self.event_id = make_event_id(self.canonical_event_key)
        return self

    @property
    def has_occurred(self) -> bool:
        return self.event_status in {EventStatus.PUBLISHED, EventStatus.REVISED}
