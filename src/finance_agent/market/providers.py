"""Provider-independent market-data interface and the Alpaca Market Data adapter.

A provider returns *provider records* tied to the immutable source snapshot they came from.
Normalization, availability, versioning and quality checks happen outside the provider, so a
provider can be replaced without touching storage or calculations.

Alpaca (https://docs.alpaca.markets): ``GET /v2/stocks/bars`` (``adjustment`` = raw | split |
dividend | spin-off | all; ``feed`` = sip | iex) and ``GET /v1/corporate-actions``. Requests are
authenticated with APCA-API-KEY-ID / APCA-API-SECRET-KEY headers, which are never logged or
stored (snapshots keep the redacted URL only).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol

from finance_agent.errors import ConfigurationError, MalformedDataError
from finance_agent.market.schemas import AdjustmentBasis, CorporateActionType
from finance_agent.providers.base import CachedFetcher
from finance_agent.storage.snapshots import SnapshotRef


@dataclass
class ProviderBar:
    provider_symbol: str
    timestamp: datetime | None  # as reported (UTC); None if missing/unparseable
    open: float | None
    high: float | None
    low: float | None
    close: float | None
    volume: float | None
    vwap: float | None
    trade_count: int | None
    snapshot: SnapshotRef
    raw: dict = field(default_factory=dict)
    missing_fields: tuple[str, ...] = ()


@dataclass
class ProviderAction:
    action_id: str
    provider_symbol: str
    action_type: CorporateActionType
    fields: dict[str, Any]
    snapshot: SnapshotRef
    raw: dict


@dataclass
class ProviderFetch:
    bars: list[ProviderBar] = field(default_factory=list)
    actions: list[ProviderAction] = field(default_factory=list)
    snapshots: list[SnapshotRef] = field(default_factory=list)
    cache_hits: int = 0


class MarketDataProvider(Protocol):
    name: str

    def daily_bars(
        self, symbol: str, start: date, end: date, adjustment: AdjustmentBasis
    ) -> ProviderFetch: ...

    def corporate_actions(self, symbol: str, start: date, end: date) -> ProviderFetch: ...


# --------------------------------------------------------------------------- parsing helpers

REQUIRED_BAR_FIELDS = ("t", "o", "h", "l", "c", "v")


def _num(v: Any) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(v: Any) -> datetime | None:
    if not isinstance(v, str) or not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC) if dt.tzinfo else None  # naive timestamps are rejected (wrong tz)


def _d(v: Any) -> date | None:
    try:
        return date.fromisoformat(v) if isinstance(v, str) and v else None
    except ValueError:
        return None


ALPACA_ACTION_TYPES: dict[str, CorporateActionType] = {
    "forward_splits": CorporateActionType.STOCK_SPLIT,
    "reverse_splits": CorporateActionType.STOCK_SPLIT,
    "cash_dividends": CorporateActionType.CASH_DIVIDEND,
    "name_changes": CorporateActionType.SYMBOL_CHANGE,
    "cash_mergers": CorporateActionType.MERGER,
    "stock_mergers": CorporateActionType.MERGER,
    "spin_offs": CorporateActionType.SPINOFF,
}
ALPACA_ADJUSTMENT = {
    AdjustmentBasis.RAW: "raw",
    AdjustmentBasis.PROVIDER_SPLIT: "split",
    AdjustmentBasis.PROVIDER_ALL: "all",
}


def parse_alpaca_bars(payload: Any, symbol: str, ref: SnapshotRef) -> tuple[list[ProviderBar], str | None]:
    """Parse ``/v2/stocks/bars`` (multi-symbol shape ``{"bars": {SYM: [...]}}``)."""
    if not isinstance(payload, dict) or "bars" not in payload:
        raise MalformedDataError("Alpaca bars payload lacks 'bars'")
    bars = payload["bars"] or {}
    rows = bars.get(symbol, []) if isinstance(bars, dict) else bars
    if not isinstance(rows, list):
        raise MalformedDataError("Alpaca bars payload: unexpected 'bars' structure")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            raise MalformedDataError("Alpaca bar is not an object")
        missing = tuple(f for f in REQUIRED_BAR_FIELDS if r.get(f) is None)
        n = r.get("n")
        out.append(
            ProviderBar(
                provider_symbol=symbol,
                timestamp=_parse_ts(r.get("t")),
                open=_num(r.get("o")),
                high=_num(r.get("h")),
                low=_num(r.get("l")),
                close=_num(r.get("c")),
                volume=_num(r.get("v")),
                vwap=_num(r.get("vw")),
                trade_count=int(n) if isinstance(n, int | float) and not isinstance(n, bool) else None,
                snapshot=ref,
                raw=r,
                missing_fields=missing,
            )
        )
    return out, payload.get("next_page_token")


def parse_alpaca_actions(
    payload: Any, symbol: str, ref: SnapshotRef
) -> tuple[list[ProviderAction], str | None]:
    if not isinstance(payload, dict) or "corporate_actions" not in payload:
        raise MalformedDataError("Alpaca corporate-actions payload lacks 'corporate_actions'")
    groups = payload["corporate_actions"] or {}
    out = []
    for key, items in groups.items():
        atype = ALPACA_ACTION_TYPES.get(key)
        if atype is None:
            continue  # unknown action class: ignored (not guessed)
        for it in items or []:
            sym = (
                it.get("symbol")
                or it.get("old_symbol")
                or it.get("source_symbol")
                or it.get("acquiree_symbol")
            )
            if sym != symbol:
                continue
            ex = it.get("ex_date") or it.get("effective_date")
            fields = {
                "ex_date": _d(ex),
                "record_date": _d(it.get("record_date")),
                "payable_date": _d(it.get("payable_date")),
                "process_date": _d(it.get("process_date")),
                "old_rate": _num(it.get("old_rate")),
                "new_rate": _num(it.get("new_rate")),
                "cash_amount": _num(it.get("rate")) if atype == CorporateActionType.CASH_DIVIDEND else None,
                "currency": it.get("currency") or "USD",
                "new_symbol": it.get("new_symbol"),
            }
            aid = (
                it.get("id")
                or hashlib.sha256(json.dumps(it, sort_keys=True, default=str).encode()).hexdigest()[:32]
            )
            out.append(ProviderAction(str(aid), symbol, atype, fields, ref, it))
    return out, payload.get("next_page_token")


# --------------------------------------------------------------------------- Alpaca adapter


class AlpacaProvider:
    name = "alpaca"
    max_pages = 50  # bounded pagination

    def __init__(
        self,
        fetcher: CachedFetcher,
        *,
        base_url: str,
        key_id: str | None,
        secret_key: str | None,
        feed: str = "sip",
        ttl_recent: timedelta = timedelta(hours=12),
        ttl_past: timedelta = timedelta(days=30),
        offline: bool = False,
    ) -> None:
        self.fetcher = fetcher
        self.base_url = base_url.rstrip("/")
        self.feed = feed
        self.ttl_recent, self.ttl_past = ttl_recent, ttl_past
        self.offline = offline
        self._headers = (
            {"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret_key} if key_id and secret_key else None
        )

    def _auth(self) -> dict[str, str] | None:
        if self._headers is None and not self.offline:
            raise ConfigurationError(
                "Alpaca market data requires ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY "
                "(free account at alpaca.markets). Use --offline for the synthetic fixtures."
            )
        return self._headers

    def _ttl(self, end: date) -> timedelta:
        return self.ttl_recent if end >= datetime.now(UTC).date() - timedelta(days=7) else self.ttl_past

    def _get(self, path: str, params: dict, source_key: str, kind: str, ttl: timedelta, refresh: bool):
        return self.fetcher.get_json(
            source_key=source_key,
            kind=kind,
            url=f"{self.base_url}{path}",
            params=params,
            ttl=ttl,
            use_cache=not refresh,
            headers=self._auth(),
        )

    def daily_bars(
        self, symbol: str, start: date, end: date, adjustment: AdjustmentBasis, *, refresh: bool = False
    ) -> ProviderFetch:
        adj = ALPACA_ADJUSTMENT.get(adjustment)
        if adj is None:
            raise ValueError(f"Alpaca adjustment for {adjustment} is not requested from the provider")
        self._auth()  # fail fast without credentials (live)
        self._auth()
        out, token = ProviderFetch(), None
        for _ in range(self.max_pages):
            params = {
                "symbols": symbol,
                "timeframe": "1Day",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "adjustment": adj,
                "feed": self.feed,
                "limit": 10000,
                "sort": "asc",
            }
            if token:
                params["page_token"] = token
            key = f"alpaca:bars:{symbol}:{adj}:{self.feed}:{start}:{end}:{token or ''}"
            payload, ref = self._get("/v2/stocks/bars", params, key, "alpaca_bars", self._ttl(end), refresh)
            out.snapshots.append(ref)
            out.cache_hits += int(ref.from_cache)
            bars, token = parse_alpaca_bars(payload, symbol, ref)
            out.bars.extend(bars)
            if not token:
                break
        else:
            raise MalformedDataError(f"Alpaca pagination exceeded {self.max_pages} pages for {symbol}")
        return out

    def corporate_actions(
        self, symbol: str, start: date, end: date, *, refresh: bool = False
    ) -> ProviderFetch:
        out, token = ProviderFetch(), None
        for _ in range(self.max_pages):
            params = {"symbols": symbol, "start": start.isoformat(), "end": end.isoformat(), "limit": 1000}
            if token:
                params["page_token"] = token
            key = f"alpaca:actions:{symbol}:{start}:{end}:{token or ''}"
            payload, ref = self._get(
                "/v1/corporate-actions", params, key, "alpaca_corporate_actions", self._ttl(end), refresh
            )
            out.snapshots.append(ref)
            out.cache_hits += int(ref.from_cache)
            actions, token = parse_alpaca_actions(payload, symbol, ref)
            out.actions.extend(actions)
            if not token:
                break
        return out
