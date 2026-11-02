"""SEC EDGAR provider: ticker→CIK, submissions, filings, XBRL companyfacts.

Official endpoints (https://www.sec.gov/search-filings/edgar-application-programming-interfaces):
  https://www.sec.gov/files/company_tickers.json
  https://data.sec.gov/submissions/CIK##########.json
  https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json
SEC fair access: declare a User-Agent with contact details; stay under 10 req/s.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from finance_agent.errors import MalformedDataError, UnknownTickerError
from finance_agent.providers.base import CachedFetcher
from finance_agent.research.concepts import SUPPORTED_CONCEPTS
from finance_agent.schemas.sec import (
    PERIODIC_FORMS,
    CompanyProfile,
    FilingMeta,
    TickerEntry,
    XbrlFact,
)
from finance_agent.storage.snapshots import SnapshotRef

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")


def normalize_ticker(ticker: str) -> str:
    t = ticker.strip().upper()
    if not TICKER_RE.match(t):
        raise UnknownTickerError(f"Invalid ticker format: {ticker!r}")
    return t


def pad_cik(cik: int | str) -> str:
    s = str(cik).strip()
    if not s.isdigit() or len(s) > 10:
        raise MalformedDataError(f"Invalid CIK: {cik!r}")
    return s.zfill(10)


# ---------------------------------------------------------------- pure parsers


def parse_company_tickers(payload: Any) -> dict[str, TickerEntry]:
    if not isinstance(payload, dict):
        raise MalformedDataError("company_tickers.json: expected an object")
    out: dict[str, TickerEntry] = {}
    for row in payload.values():
        try:
            entry = TickerEntry(
                ticker=str(row["ticker"]).upper(), cik=pad_cik(row["cik_str"]), title=str(row["title"])
            )
        except (KeyError, TypeError, MalformedDataError):
            continue
        out.setdefault(entry.ticker, entry)  # first (primary) listing wins
    if not out:
        raise MalformedDataError("company_tickers.json contained no usable rows")
    return out


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_submissions(payload: Any) -> tuple[CompanyProfile, list[FilingMeta]]:
    try:
        cik = pad_cik(payload["cik"])
        recent = payload["filings"]["recent"]
        name = payload["name"]
    except (KeyError, TypeError) as exc:
        raise MalformedDataError(f"submissions payload missing field: {exc}") from exc
    profile = CompanyProfile(
        cik=cik,
        name=name,
        tickers=list(payload.get("tickers") or []),
        exchanges=[e for e in (payload.get("exchanges") or []) if e],
        fiscal_year_end=payload.get("fiscalYearEnd") or None,
        sic=payload.get("sic") or None,
        sic_description=payload.get("sicDescription") or None,
        entity_type=payload.get("entityType") or None,
        state_of_incorporation=payload.get("stateOfIncorporation") or None,
    )
    columns = ("accessionNumber", "form", "filingDate", "reportDate", "acceptanceDateTime", "primaryDocument")
    lengths = {len(recent.get(c, [])) for c in columns if c in recent}
    if len(lengths) != 1:
        raise MalformedDataError("submissions.filings.recent columns have inconsistent lengths")
    n = lengths.pop()
    filings: list[FilingMeta] = []
    for i in range(n):
        form = recent["form"][i]
        filed = _parse_date(recent["filingDate"][i])
        if form not in PERIODIC_FORMS or filed is None:
            continue
        filings.append(
            FilingMeta(
                cik=cik,
                accession=recent["accessionNumber"][i],
                form=form,
                filing_date=filed,
                report_date=_parse_date(recent.get("reportDate", [None] * n)[i]),
                acceptance_datetime=recent.get("acceptanceDateTime", [None] * n)[i] or None,
                primary_document=recent.get("primaryDocument", [None] * n)[i] or None,
            )
        )
    filings.sort(key=lambda f: (f.filing_date, f.accession), reverse=True)
    return profile, filings


@dataclass
class CompanyFactsParse:
    cik: str
    entity_name: str
    facts: list[XbrlFact]
    skipped_malformed: int = 0
    skipped_nonperiodic: int = 0
    concepts_present: set[str] = field(default_factory=set)


def parse_companyfacts(
    payload: Any,
    concepts: frozenset[str] | set[str] = SUPPORTED_CONCEPTS,
    taxonomies: tuple[str, ...] = ("us-gaap",),
) -> CompanyFactsParse:
    """Extract facts for ``concepts`` from periodic reports (10-K/10-Q and amendments).

    Facts from other forms (8-K, proxy statements) are excluded: they are not
    period financial statements. Malformed rows are counted and skipped, never guessed.
    """
    try:
        cik = pad_cik(payload["cik"])
        facts_root = payload["facts"]
    except (KeyError, TypeError) as exc:
        raise MalformedDataError(f"companyfacts payload missing field: {exc}") from exc
    result = CompanyFactsParse(cik=cik, entity_name=str(payload.get("entityName", "")), facts=[])
    seen: set[str] = set()
    for tax in taxonomies:
        for concept, body in (facts_root.get(tax) or {}).items():
            if concept not in concepts:
                continue
            result.concepts_present.add(concept)
            for unit, rows in (body.get("units") or {}).items():
                for row in rows or []:
                    try:
                        form = row["form"]
                        if form not in PERIODIC_FORMS:
                            result.skipped_nonperiodic += 1
                            continue
                        fact = XbrlFact(
                            cik=cik,
                            taxonomy=tax,
                            concept=concept,
                            unit=unit,
                            start=_parse_date(row.get("start")),
                            end=date.fromisoformat(row["end"]),
                            value=float(row["val"]),
                            accession=row["accn"],
                            form=form,
                            filed=date.fromisoformat(row["filed"]),
                            fy=row.get("fy"),
                            fp=row.get("fp"),
                            frame=row.get("frame"),
                        )
                        if fact.start is not None and fact.start > fact.end:
                            raise ValueError("start after end")
                    except (KeyError, TypeError, ValueError):
                        result.skipped_malformed += 1
                        continue
                    h = fact.fact_hash
                    if h not in seen:  # exact duplicate rows collapse
                        seen.add(h)
                        result.facts.append(fact)
    return result


# ---------------------------------------------------------------- provider


class SecProvider:
    def __init__(self, fetcher: CachedFetcher, *, ttl_tickers: timedelta, ttl_sec: timedelta) -> None:
        self.fetcher = fetcher
        self.ttl_tickers = ttl_tickers
        self.ttl_sec = ttl_sec
        self._ticker_map: dict[str, TickerEntry] | None = None

    def ticker_map(self) -> dict[str, TickerEntry]:
        if self._ticker_map is None:
            payload, _ = self.fetcher.get_json(
                source_key="company_tickers",
                kind="ticker_map",
                url=TICKERS_URL,
                ttl=self.ttl_tickers,
                description="SEC ticker to CIK map",
            )
            self._ticker_map = parse_company_tickers(payload)
        return self._ticker_map

    def resolve_ticker(self, ticker: str) -> TickerEntry:
        t = normalize_ticker(ticker)
        mapping = self.ticker_map()
        entry = mapping.get(t) or mapping.get(t.replace(".", "-"))
        if entry is None:
            raise UnknownTickerError(f"Ticker {t} not found in SEC company_tickers.json")
        return entry

    def submissions_raw(self, cik: str) -> tuple[Any, SnapshotRef]:
        """Raw submissions JSON (shares the snapshot cache with ``submissions``)."""
        cik = pad_cik(cik)
        return self.fetcher.get_json(
            source_key=f"submissions:{cik}",
            kind="submissions",
            url=SUBMISSIONS_URL.format(cik=cik),
            ttl=self.ttl_sec,
            description=f"SEC submissions CIK{cik}",
        )

    def submissions(self, cik: str) -> tuple[CompanyProfile, list[FilingMeta], SnapshotRef]:
        payload, ref = self.submissions_raw(cik)
        profile, filings = parse_submissions(payload)
        return profile, filings, ref

    def companyfacts(self, cik: str) -> tuple[CompanyFactsParse, SnapshotRef]:
        cik = pad_cik(cik)
        payload, ref = self.fetcher.get_json(
            source_key=f"companyfacts:{cik}",
            kind="companyfacts",
            url=COMPANYFACTS_URL.format(cik=cik),
            ttl=self.ttl_sec,
            description=f"SEC XBRL companyfacts CIK{cik}",
        )
        parsed = parse_companyfacts(payload)
        if parsed.skipped_malformed:
            log.warning("skipped malformed XBRL rows", extra={"cik": cik, "count": parsed.skipped_malformed})
        return parsed, ref
