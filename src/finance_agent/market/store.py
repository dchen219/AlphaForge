"""Persistence for v0.3-A market data: versioned observations and point-in-time reads.

Versioning: every distinct provider observation is a new row (``version`` n+1); nothing is
overwritten. A later FINAL version that differs from an earlier FINAL version is a
*correction*: it becomes visible only from when it was observed (``retrieved_at``), so a
historical query never sees a correction that arrived after its cutoff.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta

from sqlalchemy import func, select

from finance_agent.market.schemas import (
    AvailabilityPrecision,
    BarStatus,
    CorporateAction,
    DataQualityIssue,
    Instrument,
    PriceBar,
    ProviderInstrumentMapping,
    QualityCheck,
    QualityStatus,
    TradingSession,
)
from finance_agent.storage.db import Database, retry_on_conflict
from finance_agent.storage.models import (
    MarketCorporateAction,
    MarketIngestionRun,
    MarketInstrument,
    MarketPriceBar,
    MarketProviderMapping,
    MarketQualityIssue,
    MarketTradingSession,
    MarketYieldObservation,
)
from finance_agent.timeutil import UTC, ensure_aware, utcnow


def _utc(dt: datetime | None) -> datetime | None:
    return ensure_aware(dt).astimezone(UTC) if dt is not None else None


def content_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def bar_hash(bar: PriceBar) -> str:
    return content_hash({"values": bar.values(), "status": str(bar.bar_status)})


def _bar(r: MarketPriceBar) -> PriceBar:
    return PriceBar(
        instrument_id=r.instrument_id,
        provider=r.provider,
        provider_symbol=r.provider_symbol,
        exchange=r.exchange,
        session_date=r.session_date,
        bar_timestamp=ensure_aware(r.bar_timestamp),
        timezone=r.timezone,
        open=r.open,
        high=r.high,
        low=r.low,
        close=r.close,
        volume=r.volume,
        vwap=r.vwap,
        trade_count=r.trade_count,
        adjustment=r.adjustment,
        currency=r.currency,
        frequency=r.frequency,
        snapshot_id=r.snapshot_id,
        snapshot_sha256=r.snapshot_sha256,
        data_origin=r.data_origin,
        retrieved_at=ensure_aware(r.retrieved_at),
        available_at=ensure_aware(r.available_at),
        availability_precision=r.availability_precision,
        bar_status=r.bar_status,
        quality_status=r.quality_status,
        version=r.version,
    )


def _action(r: MarketCorporateAction) -> CorporateAction:
    return CorporateAction.model_construct(
        action_id=r.action_id,
        provider=r.provider,
        instrument_id=r.instrument_id,
        action_type=r.action_type,
        ex_date=r.ex_date,
        record_date=r.record_date,
        payable_date=r.payable_date,
        process_date=r.process_date,
        declaration_date=r.declaration_date,
        old_rate=r.old_rate,
        new_rate=r.new_rate,
        cash_amount=r.cash_amount,
        currency=r.currency,
        new_symbol=r.new_symbol,
        snapshot_id=r.snapshot_id,
        snapshot_sha256=r.snapshot_sha256,
        data_origin=r.data_origin,
        retrieved_at=ensure_aware(r.retrieved_at),
        available_at=ensure_aware(r.available_at),
        availability_precision=r.availability_precision,
        raw=r.raw or {},
    )


class MarketStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ------------------------------------------------------------------ instruments
    @retry_on_conflict
    def register(
        self, instruments: list[Instrument], mappings: list[ProviderInstrumentMapping]
    ) -> list[DataQualityIssue]:
        issues: list[DataQualityIssue] = []
        with self.db.session() as s:
            for i in instruments:
                rec = s.get(MarketInstrument, i.instrument_id)
                values = dict(
                    name=i.name,
                    asset_class=str(i.asset_class),
                    group_name=i.group,
                    currency=i.currency,
                    primary_exchange=i.primary_exchange,
                    calendar=i.calendar,
                    active=i.active,
                )
                if rec is None:
                    s.add(MarketInstrument(instrument_id=i.instrument_id, **values))
                else:
                    for k, v in values.items():
                        setattr(rec, k, v)
            s.flush()
            for m in mappings:
                exists = s.scalar(
                    select(MarketProviderMapping.id).where(
                        MarketProviderMapping.instrument_id == m.instrument_id,
                        MarketProviderMapping.provider == m.provider,
                        MarketProviderMapping.provider_symbol == m.provider_symbol,
                    )
                )
                if exists is None:
                    s.add(MarketProviderMapping(**m.model_dump()))
            s.flush()
            dupes = s.execute(
                select(MarketProviderMapping.provider, MarketProviderMapping.provider_symbol)
                .group_by(MarketProviderMapping.provider, MarketProviderMapping.provider_symbol)
                .having(func.count(func.distinct(MarketProviderMapping.instrument_id)) > 1)
            ).all()
        for provider, sym in dupes:
            issues.append(
                DataQualityIssue(
                    check=QualityCheck.DUPLICATE_PROVIDER_SYMBOL,
                    severity="ERROR",
                    provider=provider,
                    detail=f"{provider} symbol {sym} is mapped to more than one instrument",
                )
            )
        return issues

    def instruments(self) -> list[Instrument]:
        with self.db.session() as s:
            rows = s.scalars(select(MarketInstrument).order_by(MarketInstrument.instrument_id)).all()
            return [
                Instrument(
                    instrument_id=r.instrument_id,
                    name=r.name,
                    asset_class=r.asset_class,
                    group=r.group_name,
                    currency=r.currency,
                    primary_exchange=r.primary_exchange,
                    calendar=r.calendar,
                    active=r.active,
                )
                for r in rows
            ]

    def provider_symbol(self, instrument_id: str, provider: str, on: date | None = None) -> str | None:
        with self.db.session() as s:
            rows = s.scalars(
                select(MarketProviderMapping).where(
                    MarketProviderMapping.instrument_id == instrument_id,
                    MarketProviderMapping.provider == provider,
                )
            ).all()
        for r in rows:
            if on is None or (
                (r.valid_from is None or r.valid_from <= on) and (r.valid_to is None or on <= r.valid_to)
            ):
                return r.provider_symbol
        return None

    # ------------------------------------------------------------------ sessions
    @retry_on_conflict
    def store_sessions(self, sessions: list[TradingSession]) -> int:
        added = 0
        with self.db.session() as s:
            for t in sessions:
                if s.get(MarketTradingSession, (t.calendar, t.session_date)) is None:
                    s.add(
                        MarketTradingSession(
                            calendar=t.calendar,
                            session_date=t.session_date,
                            market_open=_utc(t.market_open),
                            market_close=_utc(t.market_close),
                            pre_market_open=_utc(t.pre_market_open),
                            after_hours_close=_utc(t.after_hours_close),
                            early_close=t.early_close,
                        )
                    )
                    added += 1
        return added

    def stored_sessions(self, calendar: str, start: date, end: date) -> list[TradingSession]:
        with self.db.session() as s:
            rows = s.scalars(
                select(MarketTradingSession)
                .where(
                    MarketTradingSession.calendar == calendar,
                    MarketTradingSession.session_date >= start,
                    MarketTradingSession.session_date <= end,
                )
                .order_by(MarketTradingSession.session_date)
            ).all()
            return [
                TradingSession(
                    calendar=r.calendar,
                    session_date=r.session_date,
                    market_open=ensure_aware(r.market_open),
                    market_close=ensure_aware(r.market_close),
                    pre_market_open=ensure_aware(r.pre_market_open) if r.pre_market_open else None,
                    after_hours_close=ensure_aware(r.after_hours_close) if r.after_hours_close else None,
                    early_close=r.early_close,
                )
                for r in rows
            ]

    # ------------------------------------------------------------------ bars
    @retry_on_conflict
    def insert_bar(self, bar: PriceBar) -> str:
        """Returns inserted | unchanged | correction | finalized."""
        h = bar_hash(bar)
        with self.db.session() as s:
            versions = s.scalars(
                select(MarketPriceBar)
                .where(
                    MarketPriceBar.instrument_id == bar.instrument_id,
                    MarketPriceBar.provider == bar.provider,
                    MarketPriceBar.frequency == bar.frequency,
                    MarketPriceBar.adjustment == str(bar.adjustment),
                    MarketPriceBar.session_date == bar.session_date,
                )
                .order_by(MarketPriceBar.version)
            ).all()
            if any(v.content_hash == h for v in versions):
                return "unchanged"
            outcome = "inserted"
            available, precision = bar.available_at, bar.availability_precision
            finals = [v for v in versions if v.bar_status == BarStatus.FINAL]
            if bar.bar_status == BarStatus.FINAL and finals:
                outcome = "correction"  # provider changed a finalized observation
                available = max(_utc(available), _utc(bar.retrieved_at))
                precision = AvailabilityPrecision.OBSERVED_AT_RETRIEVAL
            elif bar.bar_status == BarStatus.FINAL and versions:
                outcome = "finalized"
            s.add(
                MarketPriceBar(
                    instrument_id=bar.instrument_id,
                    provider=bar.provider,
                    provider_symbol=bar.provider_symbol,
                    exchange=bar.exchange,
                    frequency=bar.frequency,
                    adjustment=str(bar.adjustment),
                    session_date=bar.session_date,
                    bar_timestamp=_utc(bar.bar_timestamp),
                    timezone=bar.timezone,
                    open=bar.open,
                    high=bar.high,
                    low=bar.low,
                    close=bar.close,
                    volume=bar.volume,
                    vwap=bar.vwap,
                    trade_count=bar.trade_count,
                    currency=bar.currency,
                    content_hash=h,
                    version=len(versions) + 1,
                    snapshot_id=bar.snapshot_id,
                    snapshot_sha256=bar.snapshot_sha256,
                    data_origin=bar.data_origin,
                    retrieved_at=_utc(bar.retrieved_at),
                    available_at=_utc(available),
                    availability_precision=str(precision),
                    bar_status=str(bar.bar_status),
                    quality_status=str(bar.quality_status),
                )
            )
            return outcome

    def bar_versions(self, instrument_id: str, session_date: date, adjustment: str = "raw") -> list[PriceBar]:
        with self.db.session() as s:
            rows = s.scalars(
                select(MarketPriceBar)
                .where(
                    MarketPriceBar.instrument_id == instrument_id,
                    MarketPriceBar.adjustment == adjustment,
                    MarketPriceBar.session_date == session_date,
                )
                .order_by(MarketPriceBar.version)
            ).all()
            return [_bar(r) for r in rows]

    def bars(
        self,
        instrument_id: str,
        *,
        adjustment: str = "raw",
        start: date | None = None,
        end: date | None = None,
        as_of: datetime | None = None,
        include_provisional: bool = False,
        include_quarantined: bool = False,
        provider: str | None = None,
    ) -> dict[date, PriceBar]:
        """Latest visible version per session. ``as_of`` = None means current data."""
        q = select(MarketPriceBar).where(
            MarketPriceBar.instrument_id == instrument_id, MarketPriceBar.adjustment == adjustment
        )
        if provider:
            q = q.where(MarketPriceBar.provider == provider)
        if start:
            q = q.where(MarketPriceBar.session_date >= start)
        if end:
            q = q.where(MarketPriceBar.session_date <= end)
        if as_of is not None:
            q = q.where(MarketPriceBar.available_at <= _utc(as_of))
        if not include_provisional:
            q = q.where(MarketPriceBar.bar_status == BarStatus.FINAL)
        if not include_quarantined:
            q = q.where(MarketPriceBar.quality_status != QualityStatus.QUARANTINED)
        q = q.order_by(MarketPriceBar.session_date, MarketPriceBar.version)
        out: dict[date, PriceBar] = {}
        with self.db.session() as s:
            for r in s.scalars(q).all():
                out[r.session_date] = _bar(r)  # later versions replace earlier ones
        return out

    def session_status(self, instrument_id: str, adjustment: str, start: date, end: date) -> dict[date, set]:
        """All stored (status, quality) pairs per session — used to classify gaps."""
        with self.db.session() as s:
            rows = s.execute(
                select(
                    MarketPriceBar.session_date, MarketPriceBar.bar_status, MarketPriceBar.quality_status
                ).where(
                    MarketPriceBar.instrument_id == instrument_id,
                    MarketPriceBar.adjustment == adjustment,
                    MarketPriceBar.session_date >= start,
                    MarketPriceBar.session_date <= end,
                )
            ).all()
        out: dict[date, set] = {}
        for d, st, qs in rows:
            out.setdefault(d, set()).add((st, qs))
        return out

    def last_final_session(self, instrument_id: str, provider: str, adjustment: str = "raw") -> date | None:
        with self.db.session() as s:
            return s.scalar(
                select(func.max(MarketPriceBar.session_date)).where(
                    MarketPriceBar.instrument_id == instrument_id,
                    MarketPriceBar.provider == provider,
                    MarketPriceBar.adjustment == adjustment,
                    MarketPriceBar.bar_status == BarStatus.FINAL,
                )
            )

    # ------------------------------------------------------------------ corporate actions
    @retry_on_conflict
    def insert_action(self, a: CorporateAction) -> str:
        h = content_hash(
            {
                k: getattr(a, k)
                for k in (
                    "action_type",
                    "ex_date",
                    "record_date",
                    "payable_date",
                    "process_date",
                    "declaration_date",
                    "old_rate",
                    "new_rate",
                    "cash_amount",
                    "currency",
                    "new_symbol",
                )
            }
        )
        with self.db.session() as s:
            versions = s.scalars(
                select(MarketCorporateAction)
                .where(
                    MarketCorporateAction.provider == a.provider,
                    MarketCorporateAction.action_id == a.action_id,
                )
                .order_by(MarketCorporateAction.version)
            ).all()
            if any(v.content_hash == h for v in versions):
                return "unchanged"
            available, precision = a.available_at, a.availability_precision
            if versions:  # corrected corporate action: known from when it was observed
                available = max(_utc(available), _utc(a.retrieved_at))
                precision = AvailabilityPrecision.OBSERVED_AT_RETRIEVAL
            s.add(
                MarketCorporateAction(
                    action_id=a.action_id,
                    provider=a.provider,
                    instrument_id=a.instrument_id,
                    action_type=str(a.action_type),
                    ex_date=a.ex_date,
                    record_date=a.record_date,
                    payable_date=a.payable_date,
                    process_date=a.process_date,
                    declaration_date=a.declaration_date,
                    old_rate=a.old_rate,
                    new_rate=a.new_rate,
                    cash_amount=a.cash_amount,
                    currency=a.currency,
                    new_symbol=a.new_symbol,
                    content_hash=h,
                    version=len(versions) + 1,
                    snapshot_id=a.snapshot_id,
                    snapshot_sha256=a.snapshot_sha256,
                    data_origin=a.data_origin,
                    retrieved_at=_utc(a.retrieved_at),
                    available_at=_utc(available),
                    availability_precision=str(precision),
                    raw=a.raw,
                )
            )
            return "correction" if versions else "inserted"

    def actions(self, instrument_id: str, as_of: datetime | None = None) -> list[CorporateAction]:
        q = select(MarketCorporateAction).where(MarketCorporateAction.instrument_id == instrument_id)
        if as_of is not None:
            q = q.where(MarketCorporateAction.available_at <= _utc(as_of))
        q = q.order_by(MarketCorporateAction.action_id, MarketCorporateAction.version)
        latest: dict[tuple[str, str], CorporateAction] = {}
        with self.db.session() as s:
            for r in s.scalars(q).all():
                latest[(r.provider, r.action_id)] = _action(r)
        return sorted(latest.values(), key=lambda a: (a.ex_date or date.min, a.action_id))

    # ------------------------------------------------------------------ quality issues
    @retry_on_conflict
    def add_issue(self, issue: DataQualityIssue) -> bool:
        fp = content_hash(
            {
                "check": str(issue.check),
                "instrument": issue.instrument_id,
                "provider": issue.provider,
                "session": issue.session_date,
                "detail": issue.detail,
            }
        )
        with self.db.session() as s:
            if s.scalar(select(MarketQualityIssue.id).where(MarketQualityIssue.fingerprint == fp)):
                return False
            s.add(
                MarketQualityIssue(
                    fingerprint=fp,
                    check=str(issue.check),
                    severity=issue.severity,
                    instrument_id=issue.instrument_id,
                    provider=issue.provider,
                    session_date=issue.session_date,
                    detail=issue.detail,
                    status=issue.status,
                    run_id=issue.run_id,
                )
            )
            return True

    def issues(
        self,
        *,
        instrument_id: str | None = None,
        check: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        limit: int = 500,
    ) -> list[DataQualityIssue]:
        q = select(MarketQualityIssue)
        if instrument_id:
            q = q.where(MarketQualityIssue.instrument_id == instrument_id)
        if check:
            q = q.where(MarketQualityIssue.check == check)
        if severity:
            q = q.where(MarketQualityIssue.severity == severity)
        if status:
            q = q.where(MarketQualityIssue.status == status)
        q = q.order_by(
            MarketQualityIssue.instrument_id, MarketQualityIssue.session_date, MarketQualityIssue.id
        )
        with self.db.session() as s:
            return [
                DataQualityIssue(
                    issue_id=r.id,
                    check=r.check,
                    severity=r.severity,
                    instrument_id=r.instrument_id,
                    provider=r.provider,
                    session_date=r.session_date,
                    detail=r.detail,
                    status=r.status,
                    run_id=r.run_id,
                    detected_at=ensure_aware(r.detected_at),
                )
                for r in s.scalars(q.limit(limit)).all()
            ]

    def issue_counts(self) -> dict[str, int]:
        with self.db.session() as s:
            rows = s.execute(
                select(MarketQualityIssue.check, func.count()).group_by(MarketQualityIssue.check)
            ).all()
        return {c: n for c, n in rows}

    # ------------------------------------------------------------------ runs
    @retry_on_conflict
    def save_run(self, run) -> None:
        with self.db.session() as s:
            rec = s.get(MarketIngestionRun, run.run_id)
            stats = run.model_dump(
                mode="json",
                exclude={
                    "run_id",
                    "provider",
                    "status",
                    "errors",
                    "data_origin",
                    "started_at",
                    "finished_at",
                },
            )
            if rec is None:
                s.add(
                    MarketIngestionRun(
                        run_id=run.run_id,
                        provider=run.provider,
                        params={"instruments": run.instruments, "start": str(run.start), "end": str(run.end)},
                        status=run.status,
                        stats=stats,
                        errors=run.errors,
                        data_origin=run.data_origin,
                        started_at=_utc(run.started_at),
                        finished_at=_utc(run.finished_at),
                    )
                )
            else:
                rec.status, rec.stats, rec.errors = run.status, stats, run.errors
                rec.data_origin, rec.finished_at = run.data_origin, _utc(run.finished_at)

    def runs(self, limit: int = 20) -> list[dict]:
        with self.db.session() as s:
            rows = s.scalars(
                select(MarketIngestionRun).order_by(MarketIngestionRun.started_at.desc()).limit(limit)
            ).all()
            return [
                {
                    "run_id": r.run_id,
                    "provider": r.provider,
                    "status": r.status,
                    "params": r.params,
                    "stats": r.stats,
                    "errors": r.errors,
                    "data_origin": r.data_origin,
                    "started_at": ensure_aware(r.started_at),
                    "finished_at": ensure_aware(r.finished_at) if r.finished_at else None,
                }
                for r in rows
            ]

    # ------------------------------------------------------------------ yields
    @retry_on_conflict
    def insert_yield(self, row: dict) -> str:
        h = content_hash({"value": row["value"]})
        with self.db.session() as s:
            versions = s.scalars(
                select(MarketYieldObservation)
                .where(
                    MarketYieldObservation.series_id == row["series_id"],
                    MarketYieldObservation.observation_date == row["observation_date"],
                )
                .order_by(MarketYieldObservation.version)
            ).all()
            if any(v.content_hash == h for v in versions):
                return "unchanged"
            available, precision = row["available_at"], row["availability_precision"]
            if versions:  # revised value: known from the vintage in which it was observed
                available = max(_utc(available), _utc(row["revision_available_at"]))
                precision = AvailabilityPrecision.DATE_ONLY_CONSERVATIVE
            s.add(
                MarketYieldObservation(
                    series_id=row["series_id"],
                    observation_date=row["observation_date"],
                    value=row["value"],
                    units=row["units"],
                    vintage_date=row["vintage_date"],
                    first_vintage_rule=row.get("first_vintage_rule"),
                    content_hash=h,
                    version=len(versions) + 1,
                    available_at=_utc(available),
                    availability_precision=str(precision),
                    snapshot_id=row.get("snapshot_id"),
                    snapshot_sha256=row.get("snapshot_sha256"),
                    data_origin=row["data_origin"],
                    retrieved_at=_utc(row["retrieved_at"]),
                )
            )
            return "revision" if versions else "inserted"

    def yields(
        self,
        series_id: str,
        start: date | None = None,
        end: date | None = None,
        as_of: datetime | None = None,
    ) -> list[dict]:
        q = select(MarketYieldObservation).where(MarketYieldObservation.series_id == series_id)
        if start:
            q = q.where(MarketYieldObservation.observation_date >= start)
        if end:
            q = q.where(MarketYieldObservation.observation_date <= end)
        if as_of is not None:
            q = q.where(MarketYieldObservation.available_at <= _utc(as_of))
        q = q.order_by(MarketYieldObservation.observation_date, MarketYieldObservation.version)
        latest: dict[date, dict] = {}
        with self.db.session() as s:
            for r in s.scalars(q).all():
                latest[r.observation_date] = {
                    "series_id": r.series_id,
                    "observation_date": r.observation_date,
                    "value": r.value,
                    "units": r.units,
                    "vintage_date": r.vintage_date,
                    "version": r.version,
                    "available_at": ensure_aware(r.available_at),
                    "availability_precision": r.availability_precision,
                    "snapshot_sha256": r.snapshot_sha256,
                    "data_origin": r.data_origin,
                    "retrieved_at": ensure_aware(r.retrieved_at),
                }
        return [latest[d] for d in sorted(latest)]


def day_after(d: date) -> date:
    return d + timedelta(days=1)


__all__ = ["MarketStore", "bar_hash", "content_hash", "day_after", "utcnow"]
