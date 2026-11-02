"""Schemas for SEC EDGAR entities and raw XBRL facts."""

from __future__ import annotations

import hashlib
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, computed_field

from finance_agent.timeutil import available_at

PERIODIC_FORMS = {"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-QT", "10-KT/A", "10-QT/A"}


def accession_nodash(accession: str) -> str:
    return accession.replace("-", "")


def filing_index_url(cik: str, accession: str) -> str:
    return (
        f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
        f"{accession_nodash(accession)}/{accession}-index.htm"
    )


class TickerEntry(BaseModel):
    ticker: str
    cik: str  # zero-padded to 10 digits
    title: str


class FilingMeta(BaseModel):
    accession: str
    form: str
    filing_date: date
    report_date: date | None = None
    acceptance_datetime: str | None = None
    primary_document: str | None = None
    cik: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_amendment(self) -> bool:
        return self.form.endswith("/A")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def index_url(self) -> str:
        return filing_index_url(self.cik, self.accession)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def document_url(self) -> str | None:
        if not self.primary_document:
            return None
        return (
            f"https://www.sec.gov/Archives/edgar/data/{int(self.cik)}/"
            f"{accession_nodash(self.accession)}/{self.primary_document}"
        )


class CompanyProfile(BaseModel):
    cik: str
    name: str
    tickers: list[str] = []
    exchanges: list[str] = []
    fiscal_year_end: str | None = None  # MMDD
    sic: str | None = None
    sic_description: str | None = None
    entity_type: str | None = None
    state_of_incorporation: str | None = None


class XbrlFact(BaseModel):
    """One reported XBRL fact from companyfacts, with its filing context."""

    model_config = ConfigDict(frozen=True)

    cik: str
    taxonomy: str
    concept: str
    unit: str
    start: date | None  # None for instant concepts
    end: date
    value: float
    accession: str
    form: str
    filed: date
    fy: int | None = None  # fiscal year of the FILING (not necessarily of this fact)
    fp: str | None = None  # fiscal period of the FILING
    frame: str | None = None

    @property
    def is_instant(self) -> bool:
        return self.start is None

    @property
    def duration_days(self) -> int | None:
        return None if self.start is None else (self.end - self.start).days + 1

    @property
    def available_at(self) -> datetime:
        return available_at(self.filed)

    @property
    def fact_hash(self) -> str:
        key = "|".join(
            str(x)
            for x in (
                self.cik,
                self.taxonomy,
                self.concept,
                self.unit,
                self.start,
                self.end,
                repr(self.value),
                self.accession,
            )
        )
        return hashlib.sha256(key.encode()).hexdigest()
