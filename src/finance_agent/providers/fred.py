"""FRED / ALFRED provider.

API docs: https://fred.stlouisfed.org/docs/api/fred/
- /fred/series                 series metadata
- /fred/series/observations   observations; ``realtime_start``/``realtime_end``
  select ALFRED vintages. Missing values are reported as ".".

Point-in-time strategy: for an ``as_of`` request we compute the latest vintage date
usable under the availability policy and ask ALFRED for exactly that real-time
date (realtime_start = realtime_end = D). Each returned row carries the window in
which the value was current. ``select_vintage`` applies the same rule locally to
any set of vintage rows (used for verification and replay tests).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from finance_agent.errors import ConfigurationError, MalformedDataError, NotFoundError
from finance_agent.providers.base import CachedFetcher
from finance_agent.schemas.macro import MacroSeriesMeta
from finance_agent.storage.snapshots import SnapshotRef
from finance_agent.timeutil import latest_usable_source_date

FRED_BASE = "https://api.stlouisfed.org/fred"
SERIES_RE = re.compile(r"^[A-Z0-9_]{1,30}$")
DEFAULT_SERIES = ("CPIAUCSL", "CPILFESL", "UNRATE", "PAYEMS", "DGS2", "DGS10", "FEDFUNDS", "GDP")
PAGE_LIMIT = 100000


@dataclass(frozen=True)
class VintageRow:
    observation_date: date
    value: float | None
    realtime_start: date
    realtime_end: date


def normalize_series_id(series_id: str) -> str:
    s = series_id.strip().upper()
    if not SERIES_RE.match(s):
        raise NotFoundError(f"Invalid FRED series id: {series_id!r}")
    return s


def parse_series_meta(payload: Any) -> MacroSeriesMeta:
    try:
        s = payload["seriess"][0]
        return MacroSeriesMeta(
            series_id=s["id"],
            title=s["title"],
            units=s.get("units"),
            frequency=s.get("frequency"),
            seasonal_adjustment=s.get("seasonal_adjustment"),
            last_updated=s.get("last_updated"),
            notes=s.get("notes"),
        )
    except (KeyError, IndexError, TypeError) as exc:
        raise MalformedDataError(f"FRED series payload malformed: {exc}") from exc


def parse_observations(payload: Any) -> list[VintageRow]:
    try:
        rows = payload["observations"]
    except (KeyError, TypeError) as exc:
        raise MalformedDataError(f"FRED observations payload malformed: {exc}") from exc
    out: list[VintageRow] = []
    for r in rows:
        try:
            raw = str(r["value"]).strip()
            value = None if raw in {".", ""} else float(raw)
            out.append(
                VintageRow(
                    observation_date=date.fromisoformat(r["date"]),
                    value=value,
                    realtime_start=date.fromisoformat(r["realtime_start"]),
                    realtime_end=date.fromisoformat(r["realtime_end"]),
                )
            )
        except (KeyError, ValueError, TypeError) as exc:
            raise MalformedDataError(f"Malformed FRED observation row {r!r}: {exc}") from exc
    return out


def select_vintage(rows: list[VintageRow], as_of: datetime | None) -> list[VintageRow]:
    """Return, per observation date, the value version that was usable at ``as_of``.

    A version is usable if its vintage (realtime_start) is on/before the latest
    usable source date (policy: next-day availability) and its window had not
    yet closed. Observations first published after the cutoff are excluded.
    With ``as_of=None`` the currently valid (latest) version is returned.
    """
    if as_of is None:
        cutoff = date.max
        chosen = [r for r in rows if r.realtime_end >= date(9999, 12, 31) or r.realtime_end == date.max]
        if not chosen:  # payload fetched for a specific realtime date
            chosen = rows
    else:
        cutoff = latest_usable_source_date(as_of)
        chosen = [r for r in rows if r.realtime_start <= cutoff <= r.realtime_end]
    best: dict[date, VintageRow] = {}
    for r in chosen:
        if r.realtime_start > cutoff:
            continue
        cur = best.get(r.observation_date)
        if cur is None or r.realtime_start > cur.realtime_start:
            best[r.observation_date] = r
    return [best[d] for d in sorted(best)]


class FredProvider:
    def __init__(self, fetcher: CachedFetcher, api_key: str | None, *, ttl: timedelta) -> None:
        self.fetcher = fetcher
        self.api_key = api_key
        self.ttl = ttl

    def _params(self, **extra: Any) -> dict[str, Any]:
        if not self.api_key and not self.fetcher.http.is_fixture:
            raise ConfigurationError(
                "FRED_API_KEY is not configured; live FRED/ALFRED access is unavailable. "
                "Set FRED_API_KEY or use OFFLINE_MODE=true with fixtures."
            )
        return {"api_key": self.api_key or "offline", "file_type": "json", **extra}

    def series_meta(self, series_id: str) -> tuple[MacroSeriesMeta, SnapshotRef]:
        sid = normalize_series_id(series_id)
        payload, ref = self.fetcher.get_json(
            source_key=f"series:{sid}",
            kind="fred_series",
            url=f"{FRED_BASE}/series",
            params=self._params(series_id=sid),
            ttl=self.ttl,
            description=f"FRED series {sid}",
        )
        return parse_series_meta(payload), ref

    def observations(
        self,
        series_id: str,
        *,
        as_of: datetime | None = None,
        observation_start: date | None = None,
        observation_end: date | None = None,
    ) -> tuple[list[VintageRow], SnapshotRef, date | None]:
        """Observations as known at ``as_of`` (ALFRED) or latest (``as_of=None``).

        Returns (rows, snapshot, effective_vintage_date). Offline fixtures hold the
        full vintage history and are filtered locally with :func:`select_vintage`.
        """
        sid = normalize_series_id(series_id)
        extra: dict[str, Any] = {"series_id": sid}
        vintage: date | None = None
        if as_of is not None:
            vintage = latest_usable_source_date(as_of)
            extra.update(realtime_start=vintage.isoformat(), realtime_end=vintage.isoformat())
        if observation_start:
            extra["observation_start"] = observation_start.isoformat()
        if observation_end:
            extra["observation_end"] = observation_end.isoformat()
        rows: list[VintageRow] = []
        offset = 0
        ref: SnapshotRef | None = None
        while True:
            params = self._params(**extra, limit=PAGE_LIMIT, offset=offset)
            key = (
                f"observations:{sid}:rt={vintage or 'latest'}:{observation_start}:{observation_end}:{offset}"
            )
            payload, ref = self.fetcher.get_json(
                source_key=key,
                kind="fred_observations",
                url=f"{FRED_BASE}/series/observations",
                params=params,
                ttl=self.ttl,
                description=f"FRED observations {sid}",
            )
            page = parse_observations(payload)
            rows.extend(page)
            total = int(payload.get("count", len(page)) or 0)
            offset += len(page)
            if self.fetcher.http.is_fixture or not page or offset >= total:
                break
        assert ref is not None
        selected = select_vintage(rows, as_of)
        if observation_start:
            selected = [r for r in selected if r.observation_date >= observation_start]
        if observation_end:
            selected = [r for r in selected if r.observation_date <= observation_end]
        return selected, ref, vintage
