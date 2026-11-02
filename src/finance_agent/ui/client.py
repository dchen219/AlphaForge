"""Small typed HTTP client for the AlphaForge FastAPI application.

Responses are parsed into the API's own response models, so a contract drift fails loudly here
instead of rendering half-empty pages. Every request has an explicit timeout; transport failures,
HTTP errors and malformed bodies all surface as :class:`ApiError` with a readable message.
"""

from __future__ import annotations

from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from finance_agent.interfaces.api import HealthResponse
from finance_agent.interfaces.events_api import (
    BriefingList,
    BriefingRunOut,
    EventDetail,
    EventList,
    ReplayOut,
)
from finance_agent.schemas.research import ResearchRunList, ResearchRunOut
from finance_agent.services import CompanyOverview, FinancialsResponse
from finance_agent.ui.validation import api_base_url

M = TypeVar("M", bound=BaseModel)

READ_TIMEOUT = 30.0
# Workflows run synchronously server-side; live SEC retrieval under rate limits can take minutes.
WORKFLOW_TIMEOUT = 600.0


class ApiError(Exception):
    """A failed API call. ``kind``: unreachable | timeout | http | contract."""

    def __init__(self, kind: str, message: str, *, status: int | None = None, path: str = "") -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.path = path
        self.message = message

    def __str__(self) -> str:
        code = f" {self.status}" if self.status else ""
        return f"[{self.kind}{code}] {self.path}: {self.message}"


def _detail(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:300] or resp.reason_phrase
    if isinstance(body, dict):
        d = body.get("detail", body)
        if isinstance(d, list):  # FastAPI request validation errors
            return "; ".join(f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg')}" for e in d)
        if body.get("error"):
            return f"{body['error']}: {d}"
        return str(d)
    return str(body)[:300]


class AlphaForgeClient:
    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        read_timeout: float = READ_TIMEOUT,
    ) -> None:
        self.base_url = api_base_url(base_url)
        self._http = httpx.Client(
            base_url=self.base_url,
            transport=transport,
            timeout=httpx.Timeout(read_timeout, connect=3.0),
            follow_redirects=False,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------ core
    def _call(
        self,
        method: str,
        path: str,
        model: type[M] | None,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None and v != []}
        try:
            resp = self._http.request(
                method, path, params=clean, json=json, timeout=timeout or httpx.USE_CLIENT_DEFAULT
            )
        except httpx.TimeoutException as exc:
            raise ApiError(
                "timeout", f"No response within the timeout ({type(exc).__name__}).", path=path
            ) from exc
        except httpx.TransportError as exc:
            raise ApiError(
                "unreachable",
                f"Cannot reach the API at {self.base_url} ({type(exc).__name__}). Is it running? "
                "Start it with: uv run finance-agent serve --offline",
                path=path,
            ) from exc
        if resp.status_code >= 400:
            raise ApiError("http", _detail(resp), status=resp.status_code, path=path)
        try:
            body = resp.json()
        except ValueError as exc:
            raise ApiError("contract", "Response was not JSON.", status=resp.status_code, path=path) from exc
        if model is None:
            return body
        try:
            return model.model_validate(body)
        except ValidationError as exc:
            raise ApiError(
                "contract", f"Unexpected response shape: {exc.error_count()} validation errors.", path=path
            ) from exc

    # ------------------------------------------------------------ v0.1
    def health(self) -> HealthResponse:
        return self._call("GET", "/health", HealthResponse, timeout=5.0)

    def workflows(self) -> dict[str, list[str]]:
        return self._call("GET", "/workflows", None)

    def company(self, ticker: str) -> CompanyOverview:
        return self._call("GET", f"/companies/{ticker}", CompanyOverview, timeout=WORKFLOW_TIMEOUT)

    def financials(self, ticker: str, quarters: int, as_of: str | None) -> FinancialsResponse:
        return self._call(
            "GET",
            f"/companies/{ticker}/financials",
            FinancialsResponse,
            params={"quarters": quarters, "as_of": as_of},
            timeout=WORKFLOW_TIMEOUT,
        )

    def research(
        self, ticker: str, quarters: int, as_of: str | None, use_llm: bool, question: str | None = None
    ) -> ResearchRunOut:
        body = {
            "ticker": ticker,
            "quarters": quarters,
            "as_of": as_of,
            "use_llm": use_llm,
            "question": question,
        }
        return self._call("POST", "/research/company", ResearchRunOut, json=body, timeout=WORKFLOW_TIMEOUT)

    def research_run(self, run_id: str) -> ResearchRunOut:
        return self._call("GET", f"/research/{run_id}", ResearchRunOut)

    def research_runs(self, limit: int = 50, status: str | None = None) -> ResearchRunList:
        return self._call("GET", "/research", ResearchRunList, params={"limit": limit, "status": status})

    # ------------------------------------------------------------ v0.2
    def events(
        self,
        *,
        start: str | None = None,
        end: str | None = None,
        as_of: str | None = None,
        categories: list[str] | None = None,
        subtypes: list[str] | None = None,
        ticker: str | None = None,
        levels: list[str] | None = None,
        source: str | None = None,
        limit: int = 200,
    ) -> EventList:
        params = {
            "from": start,
            "to": end,
            "as_of": as_of,
            "category": categories,
            "subtype": subtypes,
            "ticker": ticker,
            "level": levels,
            "source": source,
            "limit": limit,
        }
        return self._call("GET", "/events", EventList, params=params)

    def event(self, event_id: str, as_of: str | None = None) -> EventDetail:
        return self._call("GET", f"/events/{event_id}", EventDetail, params={"as_of": as_of})

    def calendar(self, start: str | None, end: str | None, limit: int = 200) -> EventList:
        return self._call(
            "GET", "/events/calendar", EventList, params={"from": start, "to": end, "limit": limit}
        )

    def replay(
        self,
        as_of: str,
        *,
        start: str | None = None,
        horizon_days: int = 30,
        categories: list[str] | None = None,
        ticker: str | None = None,
    ) -> ReplayOut:
        params = {
            "as_of": as_of,
            "from": start,
            "horizon_days": horizon_days,
            "category": categories,
            "ticker": ticker,
        }
        return self._call("GET", "/events/replay", ReplayOut, params=params)

    def briefing(self, **request: Any) -> BriefingRunOut:
        """POST /intelligence/briefing. Never ingests: it reads the existing event database."""
        body = {k: v for k, v in request.items() if v is not None}
        return self._call(
            "POST", "/intelligence/briefing", BriefingRunOut, json=body, timeout=WORKFLOW_TIMEOUT
        )

    def briefing_run(self, briefing_id: str) -> BriefingRunOut:
        return self._call("GET", f"/intelligence/briefings/{briefing_id}", BriefingRunOut)

    def briefings(self, limit: int = 50) -> BriefingList:
        return self._call("GET", "/intelligence/briefings", BriefingList, params={"limit": limit})
