"""Schemas for FRED/ALFRED macroeconomic data.

Four distinct dates are kept apart:
- ``observation_date``  the period the value measures (e.g. 2024-01-01 = Jan 2024)
- ``realtime_start/_end`` the ALFRED vintage window in which this value was current;
  ``realtime_start`` is the vintage date on which this value version was published
- ``publication_time``   intraday release time — NOT provided by FRED, so ``None`` (unknown)
- ``retrieved_at``      when this system fetched the data
``available_at`` applies the conservative policy in :mod:`finance_agent.timeutil`.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel


class MacroSeriesMeta(BaseModel):
    series_id: str
    title: str
    units: str | None = None
    frequency: str | None = None
    seasonal_adjustment: str | None = None
    last_updated: str | None = None
    notes: str | None = None


class MacroObservationOut(BaseModel):
    series_id: str
    observation_date: date
    value: float | None
    missing: bool
    realtime_start: date
    realtime_end: date
    vintage_date: date
    publication_time: datetime | None = None  # unknown: FRED gives dates, not times
    publication_time_known: bool = False
    available_at: datetime
    retrieved_at: datetime
    source_url: str
    snapshot_sha256: str
    data_origin: str


class MacroSeriesResponse(BaseModel):
    series: MacroSeriesMeta
    as_of: datetime | None
    vintage_policy: str
    effective_vintage_date: date | None
    observations: list[MacroObservationOut]
    data_origin: str
    warnings: list[str] = []
