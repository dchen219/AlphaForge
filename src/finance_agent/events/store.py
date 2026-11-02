"""Persistent event store: idempotent upserts, source evidence, point-in-time queries."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import Select, select

from finance_agent.events.schemas import (
    Comparison,
    EconomicChannel,
    EventStatus,
    FinancialEvent,
)
from finance_agent.storage.db import Database, retry_on_conflict
from finance_agent.storage.models import EventSourceRecord, FinancialEventRecord
from finance_agent.timeutil import ensure_aware, utcnow

log = logging.getLogger(__name__)

# Observed facts: never changed after first storage (revisions are new events).
IMMUTABLE_FIELDS = (
    "event_type",
    "event_subtype",
    "event_status",
    "series_id",
    "observation_period",
    "observation_date",
    "value",
    "previous_value",
    "unit",
    "published_at",
    "available_at",
    "occurred_at",
    "revision_status",
    "source_document_id",
)
# Derived/interpretive fields that may be recomputed on re-ingestion.
DERIVED_FIELDS = (
    "importance_score",
    "importance_level",
    "importance_components",
    "comparisons",
    "channels",
    "event_cluster_id",
    "related_event_id",
    "supersedes_event_id",
    "verification_status",
    "affected_industries",
    "description",
    "attributes",
)


@dataclass
class SourceLink:
    source_key: str
    source_name: str
    role: str
    source_url: str | None = None
    snapshot_id: int | None = None
    snapshot_sha256: str | None = None
    retrieved_at: datetime | None = None
    excerpt: str | None = None


@dataclass
class IngestStats:
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    sources_added: int = 0
    conflicts: list[str] = field(default_factory=list)

    def merge(self, other: IngestStats) -> IngestStats:
        self.inserted += other.inserted
        self.updated += other.updated
        self.unchanged += other.unchanged
        self.sources_added += other.sources_added
        self.conflicts += other.conflicts
        return self


def _utc(dt: datetime | None) -> datetime | None:
    """SQLite keeps wall-clock time only: always store and compare in UTC."""
    return None if dt is None else ensure_aware(dt).astimezone(UTC)


DATETIME_FIELDS = (
    "occurred_at",
    "scheduled_for",
    "published_at",
    "available_at",
    "ingested_at",
    "created_at",
    "updated_at",
)


def _to_record_values(ev: FinancialEvent) -> dict:
    d = ev.model_dump(mode="python")
    for k in DATETIME_FIELDS:
        d[k] = _utc(d.get(k))
    d["comparisons"] = [c.model_dump(mode="json") for c in ev.comparisons]
    d["channels"] = [c.model_dump(mode="json") for c in ev.channels]
    d["attributes"] = ev.model_dump(mode="json")["attributes"]
    d["importance_components"] = ev.model_dump(mode="json")["importance_components"]
    for k in (
        "event_type",
        "event_subtype",
        "event_status",
        "availability_precision",
        "consensus_status",
        "unit",
        "revision_status",
        "importance_level",
        "verification_status",
    ):
        if d.get(k) is not None:
            d[k] = str(d[k])
    return d


def record_to_event(r: FinancialEventRecord) -> FinancialEvent:
    def aware(x: datetime | None) -> datetime | None:
        return ensure_aware(x) if x is not None else None

    return FinancialEvent(
        event_id=r.event_id,
        canonical_event_key=r.canonical_event_key,
        event_type=r.event_type,
        event_subtype=r.event_subtype,
        event_status=r.event_status,
        title=r.title,
        description=r.description,
        source_name=r.source_name,
        source_url=r.source_url,
        source_document_id=r.source_document_id,
        source_snapshot_hash=r.source_snapshot_hash,
        data_origin=r.data_origin,
        affected_entities=r.affected_entities or [],
        affected_industries=r.affected_industries or [],
        occurred_at=aware(r.occurred_at),
        scheduled_for=aware(r.scheduled_for),
        published_at=aware(r.published_at),
        available_at=aware(r.available_at),
        ingested_at=aware(r.ingested_at),
        availability_precision=r.availability_precision,
        series_id=r.series_id,
        observation_period=r.observation_period,
        observation_date=r.observation_date,
        value=r.value,
        previous_value=r.previous_value,
        consensus_value=r.consensus_value,
        consensus_status=r.consensus_status,
        unit=r.unit,
        revision_status=r.revision_status,
        importance_score=r.importance_score,
        importance_level=r.importance_level,
        importance_components=r.importance_components or {},
        verification_status=r.verification_status,
        related_event_id=r.related_event_id,
        supersedes_event_id=r.supersedes_event_id,
        event_cluster_id=r.event_cluster_id,
        comparisons=[Comparison.model_validate(c) for c in r.comparisons or []],
        channels=[EconomicChannel.model_validate(c) for c in r.channels or []],
        attributes=r.attributes or {},
        created_at=aware(r.created_at),
        updated_at=aware(r.updated_at),
    )


def _same(a, b) -> bool:
    if isinstance(a, datetime) and isinstance(b, datetime):
        return ensure_aware(a) == ensure_aware(b)
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
    return a == b


class EventStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    @retry_on_conflict
    def upsert(self, event: FinancialEvent, sources: list[SourceLink] | None = None) -> IngestStats:
        stats = IngestStats()
        now = utcnow()
        values = _to_record_values(event)
        with self.db.session() as s:
            rec = s.get(FinancialEventRecord, event.event_id)
            if rec is None:
                values.update(ingested_at=_utc(event.ingested_at) or now, created_at=now, updated_at=now)
                s.add(FinancialEventRecord(**values))
                s.flush()
                stats.inserted = 1
            else:
                for f in IMMUTABLE_FIELDS:
                    if not _same(getattr(rec, f), values[f]):
                        stats.conflicts.append(
                            f"{event.event_id} {event.canonical_event_key}: {f} stored={getattr(rec, f)!r} "
                            f"incoming={values[f]!r} (stored value kept; observed facts are immutable)"
                        )
                changed = False
                for f in DERIVED_FIELDS:
                    if not _same(getattr(rec, f), values[f]):
                        setattr(rec, f, values[f])
                        changed = True
                if changed:
                    rec.updated_at = now
                    stats.updated = 1
                else:
                    stats.unchanged = 1
            for src in sources or []:
                exists = s.scalar(
                    select(EventSourceRecord.id).where(
                        EventSourceRecord.event_id == event.event_id,
                        EventSourceRecord.source_key == src.source_key,
                    )
                )
                if exists is None:
                    s.add(EventSourceRecord(event_id=event.event_id, **src.__dict__))
                    stats.sources_added += 1
        for c in stats.conflicts:
            log.warning("event conflict", extra={"detail": c})
        return stats

    # ---------------------------------------------------------------- queries
    def get(self, event_id: str) -> FinancialEvent | None:
        with self.db.session() as s:
            r = s.get(FinancialEventRecord, event_id)
            return record_to_event(r) if r else None

    def by_key(self, canonical_event_key: str) -> FinancialEvent | None:
        with self.db.session() as s:
            r = s.scalar(
                select(FinancialEventRecord).where(
                    FinancialEventRecord.canonical_event_key == canonical_event_key
                )
            )
            return record_to_event(r) if r else None

    def sources(self, event_id: str) -> list[EventSourceRecord]:
        with self.db.session() as s:
            rows = s.scalars(
                select(EventSourceRecord)
                .where(EventSourceRecord.event_id == event_id)
                .order_by(EventSourceRecord.id)
            ).all()
            s.expunge_all()
            return list(rows)

    def query(
        self,
        *,
        as_of: datetime | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        categories: list[str] | None = None,
        subtypes: list[str] | None = None,
        statuses: list[str] | None = None,
        ticker: str | None = None,
        levels: list[str] | None = None,
        verification: list[str] | None = None,
        source: str | None = None,
        series_id: str | None = None,
        limit: int = 500,
        occurred_only: bool = True,
    ) -> list[FinancialEvent]:
        """Point-in-time query over *available* events.

        ``occurred_only`` (default) returns only PUBLISHED/REVISED events, filtered on
        ``available_at`` (never on observation period). ``as_of`` hides everything that
        became available later. Scheduled events are only returned by ``calendar``.
        """
        q: Select = select(FinancialEventRecord)
        if occurred_only:
            q = q.where(FinancialEventRecord.event_status.in_([EventStatus.PUBLISHED, EventStatus.REVISED]))
            q = q.where(FinancialEventRecord.available_at.is_not(None))
            if as_of is not None:
                q = q.where(FinancialEventRecord.available_at <= _utc(as_of))
            if start is not None:
                q = q.where(FinancialEventRecord.available_at > _utc(start))
            if end is not None:
                q = q.where(FinancialEventRecord.available_at <= _utc(end))
        if categories:
            q = q.where(FinancialEventRecord.event_type.in_(categories))
        if subtypes:
            q = q.where(FinancialEventRecord.event_subtype.in_(subtypes))
        if statuses:
            q = q.where(FinancialEventRecord.event_status.in_(statuses))
        if levels:
            q = q.where(FinancialEventRecord.importance_level.in_(levels))
        if verification:
            q = q.where(FinancialEventRecord.verification_status.in_(verification))
        if source:
            q = q.where(FinancialEventRecord.source_name == source)
        if series_id:
            q = q.where(FinancialEventRecord.series_id == series_id)
        q = q.order_by(FinancialEventRecord.available_at.desc(), FinancialEventRecord.event_id).limit(
            limit * 4
        )
        with self.db.session() as s:
            rows = [record_to_event(r) for r in s.scalars(q).all()]
        if ticker:
            t = ticker.upper()
            rows = [e for e in rows if t in e.affected_entities]
        return rows[:limit]

    def calendar(self, *, start: datetime, end: datetime, limit: int = 500) -> list[FinancialEvent]:
        """Scheduled entries with ``scheduled_for`` in (start, end]. Not evidence of occurrence."""
        q = (
            select(FinancialEventRecord)
            .where(FinancialEventRecord.event_status == EventStatus.SCHEDULED)
            .where(FinancialEventRecord.scheduled_for > _utc(start))
            .where(FinancialEventRecord.scheduled_for <= _utc(end))
            .order_by(FinancialEventRecord.scheduled_for, FinancialEventRecord.event_id)
            .limit(limit)
        )
        with self.db.session() as s:
            return [record_to_event(r) for r in s.scalars(q).all()]

    def cluster_members(self, cluster_id: str) -> list[FinancialEvent]:
        with self.db.session() as s:
            rows = s.scalars(
                select(FinancialEventRecord).where(FinancialEventRecord.event_cluster_id == cluster_id)
            ).all()
            return [record_to_event(r) for r in rows]

    def latest_version(self, key_prefix: str, as_of: datetime | None = None) -> FinancialEvent | None:
        """Most recent available event whose canonical key starts with ``key_prefix``."""
        q = select(FinancialEventRecord).where(
            FinancialEventRecord.canonical_event_key.startswith(key_prefix)
        )
        if as_of is not None:
            q = q.where(FinancialEventRecord.available_at <= _utc(as_of))
        q = q.order_by(FinancialEventRecord.available_at.desc()).limit(1)
        with self.db.session() as s:
            r = s.scalar(q)
            return record_to_event(r) if r else None


class BriefingStore:
    """Persistence for briefings and their ranked event references."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, briefing_id: str, as_of: datetime, window_start: datetime, params: dict) -> None:
        from finance_agent.storage.models import BriefingRecord

        with self.db.session() as s:
            s.add(
                BriefingRecord(
                    briefing_id=briefing_id,
                    as_of=_utc(as_of),
                    window_start=_utc(window_start),
                    window_end=_utc(as_of),
                    status="running",
                    params=params,
                    steps=[],
                )
            )

    def update(self, briefing_id: str, **fields) -> None:
        from finance_agent.storage.models import BriefingRecord

        with self.db.session() as s:
            rec = s.get(BriefingRecord, briefing_id)
            if rec is not None:
                for k, v in fields.items():
                    setattr(rec, k, v)

    def save_events(self, briefing_id: str, rows: list[tuple[str, str, int, float | None]]) -> None:
        from finance_agent.storage.models import BriefingEventRecord

        with self.db.session() as s:
            for event_id, section, rank, score in rows:
                s.add(
                    BriefingEventRecord(
                        briefing_id=briefing_id,
                        event_id=event_id,
                        section=section,
                        rank=rank,
                        importance_score=score,
                    )
                )

    def get(self, briefing_id: str):
        from finance_agent.storage.models import BriefingRecord

        with self.db.session() as s:
            rec = s.get(BriefingRecord, briefing_id)
            if rec is not None:
                s.expunge(rec)
            return rec
