"""Content-addressed, immutable source snapshot store with read-through caching."""

from __future__ import annotations

import gzip
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select

from finance_agent.errors import IntegrityError
from finance_agent.http import FetchResult
from finance_agent.storage.db import Database, retry_on_conflict
from finance_agent.storage.models import SourceDocument, SourceSnapshot
from finance_agent.timeutil import ensure_aware, utcnow


@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: int
    sha256: str
    retrieved_at: datetime
    origin: str
    url: str
    from_cache: bool


class SnapshotStore:
    def __init__(self, db: Database, root: Path) -> None:
        self.db = db
        self.root = root

    def _path(self, sha256: str) -> Path:
        return self.root / sha256[:2] / f"{sha256}.json.gz"

    def _write_blob(self, content: bytes, sha256: str) -> Path:
        path = self._path(sha256)
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unique temp name per writer: concurrent writers of the same content must not
        # share a temp file (on Windows that raises PermissionError).
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        with gzip.open(tmp, "wb") as fh:
            fh.write(content)
        try:
            tmp.replace(path)
        except OSError:
            # Another writer won the race; content is identical (same SHA-256 address).
            tmp.unlink(missing_ok=True)
            if not path.exists():
                raise
        return path

    def read_blob(self, sha256: str, storage_path: str | None = None) -> bytes:
        path = Path(storage_path) if storage_path else self._path(sha256)
        with gzip.open(path, "rb") as fh:
            content = fh.read()
        actual = hashlib.sha256(content).hexdigest()
        if actual != sha256:
            raise IntegrityError(f"Snapshot {path} hash mismatch: expected {sha256}, got {actual}")
        return content

    @retry_on_conflict
    def save(
        self,
        *,
        provider: str,
        source_key: str,
        kind: str,
        result: FetchResult,
        description: str | None = None,
    ) -> SnapshotRef:
        path = self._write_blob(result.content, result.sha256)
        with self.db.session() as s:
            doc = s.scalar(
                select(SourceDocument).where(
                    SourceDocument.provider == provider, SourceDocument.source_key == source_key
                )
            )
            if doc is None:
                doc = SourceDocument(
                    provider=provider,
                    source_key=source_key,
                    url=result.url,
                    kind=kind,
                    description=description,
                )
                s.add(doc)
                s.flush()
            snap = s.scalar(
                select(SourceSnapshot).where(
                    SourceSnapshot.source_document_id == doc.id,
                    SourceSnapshot.sha256 == result.sha256,
                )
            )
            if snap is not None:
                # Content is immutable; only the retrieval metadata advances so that an
                # unchanged re-fetch counts as fresh for the cache TTL.
                snap.last_retrieved_at = result.retrieved_at
            else:  # identical content is deduplicated, never overwritten
                snap = SourceSnapshot(
                    source_document_id=doc.id,
                    sha256=result.sha256,
                    retrieved_at=result.retrieved_at,
                    storage_path=str(path),
                    byte_size=len(result.content),
                    http_status=result.status_code,
                    content_type=result.content_type,
                    origin=result.origin,
                    last_retrieved_at=result.retrieved_at,
                )
                s.add(snap)
                s.flush()
            return SnapshotRef(
                snap.id, snap.sha256, ensure_aware(snap.retrieved_at), snap.origin, doc.url, False
            )

    def latest_fresh(
        self, provider: str, source_key: str, ttl: timedelta, origin_prefix: str
    ) -> tuple[SnapshotRef, bytes] | None:
        """Most recent snapshot within TTL whose origin matches the current mode."""
        with self.db.session() as s:
            row = s.execute(
                select(SourceSnapshot, SourceDocument)
                .join(SourceDocument)
                .where(SourceDocument.provider == provider, SourceDocument.source_key == source_key)
                .where(SourceSnapshot.origin.startswith(origin_prefix))
                .order_by(func.coalesce(SourceSnapshot.last_retrieved_at, SourceSnapshot.retrieved_at).desc())
                .limit(1)
            ).first()
        if row is None:
            return None
        snap, doc = row
        last_seen = snap.last_retrieved_at or snap.retrieved_at
        if utcnow() - ensure_aware(last_seen) > ttl:
            return None
        content = self.read_blob(snap.sha256, snap.storage_path)
        ref = SnapshotRef(snap.id, snap.sha256, ensure_aware(snap.retrieved_at), snap.origin, doc.url, True)
        return ref, content
