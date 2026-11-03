"""Persistence for event-study runs. Finalized runs are immutable.

``save`` inserts a run once; saving a run whose ``run_id`` already exists returns the stored run
untouched (identical spec + data + methodology). Changed inputs give a different run id, so
earlier results are never overwritten.
"""

from __future__ import annotations

from sqlalchemy import select

from finance_agent.event_studies.schemas import EventStudyRun, StudyStatus
from finance_agent.storage.db import Database, retry_on_conflict
from finance_agent.storage.models import EventStudyResultRecord, EventStudyRunRecord
from finance_agent.timeutil import ensure_aware, utcnow


def _run(r: EventStudyRunRecord, existing: bool = False) -> EventStudyRun:
    return EventStudyRun(
        run_id=r.run_id, kind=r.kind, status=StudyStatus(r.status), spec_hash=r.spec_hash,
        data_digest=r.data_digest, specification=r.specification, event_ids=r.event_ids or [],
        data_origin=r.data_origin, synthetic=r.synthetic, created_at=ensure_aware(r.created_at),
        methodology_version=r.methodology_version, software=r.software or {}, result=r.result or {},
        report_markdown=r.report_markdown, existing=existing,
    )  # fmt: skip


class EventStudyStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    @retry_on_conflict
    def save(self, run: EventStudyRun, rows: list[dict], manifest: dict) -> EventStudyRun:
        with self.db.session() as s:
            prev = s.get(EventStudyRunRecord, run.run_id)
            if prev is not None:
                return _run(prev, existing=True)
            s.add(
                EventStudyRunRecord(
                    run_id=run.run_id, kind=run.kind, status=str(run.status), spec_hash=run.spec_hash,
                    data_digest=run.data_digest, methodology_version=run.methodology_version,
                    specification=run.specification, event_ids=run.event_ids,
                    data_mode=str(run.specification.get("data_mode", "research")),
                    data_origin=run.data_origin, synthetic=run.synthetic, software=run.software,
                    data_manifest=manifest, result=run.result, report_markdown=run.report_markdown,
                    created_at=run.created_at, finalized_at=utcnow(),
                )
            )  # fmt: skip
            s.flush()
            for row in rows:
                s.add(EventStudyResultRecord(run_id=run.run_id, **row))
        return run

    def get(self, run_id: str) -> EventStudyRun | None:
        with self.db.session() as s:
            r = s.get(EventStudyRunRecord, run_id)
            return _run(r) if r else None

    def manifest(self, run_id: str) -> dict | None:
        with self.db.session() as s:
            r = s.get(EventStudyRunRecord, run_id)
            return r.data_manifest if r else None

    def results(self, run_id: str) -> list[dict]:
        with self.db.session() as s:
            rows = s.scalars(
                select(EventStudyResultRecord)
                .where(EventStudyResultRecord.run_id == run_id)
                .order_by(EventStudyResultRecord.id)
            ).all()
            return [
                {
                    c.name: getattr(r, c.name)
                    for c in EventStudyResultRecord.__table__.columns
                    if c.name != "id"
                }
                for r in rows
            ]

    def list(self, limit: int = 50) -> list[dict]:
        with self.db.session() as s:
            rows = s.scalars(
                select(EventStudyRunRecord)
                .order_by(EventStudyRunRecord.created_at.desc())
                .limit(min(limit, 500))
            ).all()
            return [
                {
                    "run_id": r.run_id,
                    "kind": r.kind,
                    "status": r.status,
                    "event_ids": (r.event_ids or [])[:5],
                    "target": (r.specification or {}).get("target"),
                    "data_origin": r.data_origin,
                    "synthetic": r.synthetic,
                    "created_at": ensure_aware(r.created_at),
                }
                for r in rows
            ]
