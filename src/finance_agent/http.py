"""HTTP access with timeouts, retries, per-host rate limiting and offline fixtures."""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from finance_agent.config.logging import redact
from finance_agent.errors import (
    MalformedDataError,
    NotFoundError,
    ProviderError,
    ProviderHTTPError,
    RateLimitedError,
)
from finance_agent.timeutil import utcnow

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class FetchResult:
    url: str  # redacted — safe to store and log
    status_code: int
    content: bytes
    retrieved_at: datetime
    origin: str  # "live" | "fixture:<kind>"
    content_type: str | None = None
    sha256: str = field(init=False)

    def __post_init__(self) -> None:
        self.sha256 = hashlib.sha256(self.content).hexdigest()

    def text(self) -> str:
        try:
            return self.content.decode("utf-8")
        except UnicodeDecodeError:
            return self.content.decode("latin-1")

    def json(self) -> Any:
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise MalformedDataError(f"Invalid JSON from {self.url}: {exc}") from exc


class RateLimiter:
    """Minimal thread-safe minimum-interval limiter."""

    def __init__(self, max_per_second: float) -> None:
        self.interval = 1.0 / max_per_second if max_per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            wait = self._next - now
            self._next = max(now, self._next) + self.interval
        if wait > 0:
            time.sleep(wait)


class FixtureTransport(httpx.BaseTransport):
    """Serves provider URLs from files under a fixtures directory (offline mode).

    URL → file mapping:
      www.sec.gov/files/company_tickers.json        → sec/company_tickers.json
      data.sec.gov/submissions/CIK##########.json   → sec/submissions/CIK##########.json
      data.sec.gov/api/xbrl/companyfacts/CIK…json   → sec/companyfacts/CIK##########.json
      api.stlouisfed.org/fred/series?series_id=X    → fred/series/X.json
      …/fred/series/observations?series_id=X        → fred/observations/X.json
    ``MANIFEST.json`` records whether each fixture is a real snapshot or synthetic.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        manifest = root / "MANIFEST.json"
        self.manifest: dict[str, Any] = (
            json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
        )

    def resolve(self, url: httpx.URL) -> Path | None:
        host, path = url.host, url.path
        qs = parse_qs(urlparse(str(url)).query)
        if host == "www.sec.gov" and path == "/files/company_tickers.json":
            return self.root / "sec" / "company_tickers.json"
        if host == "data.sec.gov" and path.startswith("/submissions/"):
            return self.root / "sec" / "submissions" / Path(path).name
        if host == "data.sec.gov" and path.startswith("/api/xbrl/companyfacts/"):
            return self.root / "sec" / "companyfacts" / Path(path).name
        if host == "api.stlouisfed.org":
            sid = (qs.get("series_id") or [""])[0].upper()
            if path == "/fred/series":
                return self.root / "fred" / "series" / f"{sid}.json"
            if path == "/fred/series/observations":
                return self.root / "fred" / "observations" / f"{sid}.json"
        # ---- v0.2 official event sources
        if host == "www.federalreserve.gov":
            if path == "/feeds/press_monetary.xml":
                return self.root / "fed" / "press_monetary.xml"
            if path == "/monetarypolicy/fomccalendars.htm":
                return self.root / "fed" / "fomccalendars.htm"
            if path.startswith("/newsevents/pressreleases/") and path.endswith(".htm"):
                return self.root / "fed" / "statements" / Path(path).name
        if host == "www.bls.gov" and path == "/schedule/news_release/bls.ics":
            return self.root / "bls" / "bls.ics"
        if host == "www.bea.gov" and path == "/news/schedule":
            return self.root / "bea" / "schedule.htm"
        if host == "alfred.stlouisfed.org" and path == "/graph/alfredgraph.csv":
            sid = (qs.get("id") or [""])[0].upper()
            vintage = (qs.get("vintage_date") or [""])[0]
            if sid and vintage:
                return self.root / "alfred" / sid / f"{vintage}.csv"
        return None

    def origin_for(self, path: Path) -> str:
        rel = path.relative_to(self.root).as_posix()
        for prefix, meta in self.manifest.get("groups", {}).items():
            if rel.startswith(prefix):
                return f"fixture:{meta.get('kind', 'unknown')}"
        return "fixture:unknown"

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        path = self.resolve(request.url)
        if path is None or not path.exists():
            return httpx.Response(404, json={"error": "offline fixture not found"}, request=request)
        return httpx.Response(
            200,
            content=path.read_bytes(),
            headers={"content-type": "application/json", "x-fixture-origin": self.origin_for(path)},
            request=request,
        )


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout: float = 30.0,
        max_attempts: int = 4,
        rate_limits: dict[str, float] | None = None,
        transport: httpx.BaseTransport | None = None,
        backoff_base: float = 0.5,
        sleep=time.sleep,
    ) -> None:
        self._client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=httpx.Timeout(timeout, connect=min(10.0, timeout)),
            transport=transport,
            follow_redirects=True,
        )
        self.max_attempts = max(1, max_attempts)
        self.backoff_base = backoff_base
        self.is_fixture = isinstance(transport, FixtureTransport)
        # Rate limits protect live providers; offline fixture replay reads local files.
        self._limiters = (
            {} if self.is_fixture else {h: RateLimiter(r) for h, r in (rate_limits or {}).items()}
        )
        self._sleep = sleep
        self.request_count = 0

    def close(self) -> None:
        self._client.close()

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                return min(60.0, float(retry_after))
            except ValueError:
                pass
        return min(30.0, self.backoff_base * (2**attempt)) + random.uniform(0, 0.25)

    def get(self, url: str, params: dict[str, Any] | None = None) -> FetchResult:
        host = httpx.URL(url).host
        limiter = self._limiters.get(host)
        last_exc: Exception | None = None
        for attempt in range(self.max_attempts):
            if limiter:
                limiter.acquire()
            try:
                self.request_count += 1
                resp = self._client.get(url, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                log.warning(
                    "http transport error; retrying",
                    extra={"url": redact(url), "attempt": attempt + 1, "error": type(exc).__name__},
                )
                if attempt + 1 < self.max_attempts:
                    self._sleep(self._backoff(attempt, None))
                continue
            safe_url = redact(str(resp.request.url))
            if resp.status_code in RETRYABLE_STATUS:
                last_exc = ProviderHTTPError(f"HTTP {resp.status_code} from {safe_url}", resp.status_code)
                log.warning(
                    "retryable http status",
                    extra={"url": safe_url, "status": resp.status_code, "attempt": attempt + 1},
                )
                if attempt + 1 < self.max_attempts:
                    self._sleep(self._backoff(attempt, resp.headers.get("retry-after")))
                continue
            if resp.status_code == 404:
                where = "offline fixtures" if self.is_fixture else "provider"
                raise NotFoundError(f"Not found in {where}: {safe_url}")
            if resp.status_code >= 400:
                raise ProviderHTTPError(
                    f"HTTP {resp.status_code} from {safe_url}: {redact(resp.text[:300])}",
                    resp.status_code,
                )
            return FetchResult(
                url=safe_url,
                status_code=resp.status_code,
                content=resp.content,
                retrieved_at=utcnow(),
                origin=resp.headers.get("x-fixture-origin", "live"),
                content_type=resp.headers.get("content-type"),
            )
        if isinstance(last_exc, ProviderHTTPError) and last_exc.status_code == 429:
            raise RateLimitedError(str(last_exc), 429)
        if isinstance(last_exc, ProviderHTTPError):
            raise last_exc
        raise ProviderError(
            f"Request failed after {self.max_attempts} attempts: {redact(url)} ({last_exc!r})"
        )
