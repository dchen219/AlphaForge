"""Shared provider plumbing: fetch-through-snapshot-cache."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from finance_agent.errors import MalformedDataError
from finance_agent.http import HttpClient
from finance_agent.storage.snapshots import SnapshotRef, SnapshotStore

log = logging.getLogger(__name__)


class CachedFetcher:
    """Fetch JSON, persisting every response as an immutable snapshot.

    A fresh snapshot (within TTL) from the same mode (live vs fixture) is reused,
    which keeps SEC/FRED request volume low and makes reruns reproducible.
    """

    def __init__(self, provider: str, http: HttpClient, snapshots: SnapshotStore) -> None:
        self.provider = provider
        self.http = http
        self.snapshots = snapshots

    def get_json(
        self,
        *,
        source_key: str,
        kind: str,
        url: str,
        params: dict[str, Any] | None = None,
        ttl: timedelta,
        description: str | None = None,
        use_cache: bool = True,
    ) -> tuple[Any, SnapshotRef]:
        mode_prefix = "fixture" if self.http.is_fixture else "live"
        if use_cache:
            cached = self.snapshots.latest_fresh(self.provider, source_key, ttl, mode_prefix)
            if cached is not None:
                ref, content = cached
                log.info("snapshot cache hit", extra={"source_key": source_key, "sha256": ref.sha256})
                try:
                    return json.loads(content), ref
                except ValueError as exc:
                    raise MalformedDataError(f"Cached snapshot for {source_key} is not JSON") from exc
        result = self.http.get(url, params=params)
        payload = result.json()
        ref = self.snapshots.save(
            provider=self.provider,
            source_key=source_key,
            kind=kind,
            result=result,
            description=description,
        )
        return payload, ref
