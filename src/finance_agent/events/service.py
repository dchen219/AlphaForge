"""Event ingestion orchestration (manual / scheduler-invoked; no daemon)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from finance_agent.errors import FinanceAgentError
from finance_agent.events.importance import ImportanceConfig, apply_importance
from finance_agent.events.schemas import EventStatus, FinancialEvent
from finance_agent.events.sources import calendars, fomc
from finance_agent.events.sources.macro import AlfredClient, MacroEventBuilder
from finance_agent.events.sources.sec_events import SecEventBuilder, parse_submission_filings
from finance_agent.events.store import EventStore, IngestStats, SourceLink
from finance_agent.events.transmission import channels_for
from finance_agent.providers.base import CachedFetcher
from finance_agent.research.calculations import compute_quarterly_metrics
from finance_agent.storage.snapshots import SnapshotRef
from finance_agent.timeutil import ET, available_at, ensure_aware, utcnow

log = logging.getLogger(__name__)
DEFAULT_TICKERS = ("MSFT", "AAPL", "AMZN", "GOOGL", "META")
SOURCES = ("sec", "fomc", "macro")


def fixture_event_window(fixtures_dir) -> tuple[date, datetime] | None:
    """(start, as_of) the real v0.2 fixtures were recorded with (tests/fixtures/MANIFEST.json)."""
    import json
    from pathlib import Path

    path = Path(fixtures_dir) / "MANIFEST.json"
    if not path.exists():
        return None
    w = json.loads(path.read_text(encoding="utf-8")).get("event_window")
    if not w:
        return None
    return date.fromisoformat(w["start"]), datetime.fromisoformat(w["as_of"])


def fixture_retrieved_at(fixtures_dir, group: str) -> datetime | None:
    """When a recorded fixture group was really retrieved (MANIFEST.json → groups → retrieved_at)."""
    import json
    from pathlib import Path

    path = Path(fixtures_dir) / "MANIFEST.json"
    if not path.exists():
        return None
    raw = json.loads(path.read_text(encoding="utf-8")).get("groups", {}).get(group, {}).get("retrieved_at")
    return datetime.fromisoformat(raw) if raw else None


@dataclass
class IngestReport:
    as_of: datetime | None
    start: date
    per_source: dict[str, dict] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat() if self.as_of else None,
            "start": self.start.isoformat(),
            "per_source": self.per_source,
            "errors": self.errors,
            "ok": self.ok,
        }


class EventIngestor:
    def __init__(self, ctx, store: EventStore | None = None, cfg: ImportanceConfig | None = None) -> None:
        self.ctx = ctx
        self.store = store or EventStore(ctx.db)
        self.cfg = cfg or ImportanceConfig()
        s = ctx.settings
        self.official = CachedFetcher("official", ctx.http, ctx.snapshots)
        self.alfred = AlfredClient(
            self.official,
            ttl_past=timedelta(hours=s.cache_ttl_alfred_past_hours),
            ttl_today=timedelta(hours=s.cache_ttl_alfred_today_hours),
        )
        self.calendar_ttl = timedelta(hours=s.cache_ttl_calendar_hours)

    # ------------------------------------------------------------ common
    def _observed(self, ref: SnapshotRef | None, group: str) -> SnapshotRef | None:
        """Calendar snapshot ref whose ``retrieved_at`` is when the schedule was really observed.

        Calendars are not versioned historically, so a schedule is knowledge only from the time
        it was retrieved. Offline, that is the recording time in MANIFEST.json, not replay time.
        """
        if ref is None or not self.ctx.http.is_fixture:
            return ref
        when = fixture_retrieved_at(self.ctx.settings.fixtures_dir, group)
        return replace(ref, retrieved_at=when) if when else ref

    def _store(
        self, items: list[tuple[FinancialEvent, list[SourceLink]]], as_of: datetime | None
    ) -> IngestStats:
        stats = IngestStats()
        for ev, links in items:
            if ev.has_occurred and as_of is not None and (ev.available_at is None or ev.available_at > as_of):
                continue  # historical ingestion never stores post-cutoff information
            if ev.event_status == EventStatus.REVISED and ev.supersedes_event_id is None:
                prefix = ev.attributes.get("supersedes_key_prefix")
                prior = (
                    self.store.latest_version(prefix, as_of=ev.available_at - timedelta(microseconds=1))
                    if prefix
                    else None
                )
                ev.supersedes_event_id = prior.event_id if prior else None
            apply_importance(ev, self.cfg)
            ev.channels = channels_for(ev)
            stats.merge(self.store.upsert(ev, links))
        return stats

    def run(
        self,
        sources: list[str] | None = None,
        *,
        as_of: datetime | None = None,
        start: date | None = None,
        tickers: list[str] | None = None,
    ) -> IngestReport:
        as_of = ensure_aware(as_of) if as_of else None
        ref_day = (as_of or utcnow()).astimezone(ET).date()
        start = start or (ref_day - timedelta(days=120))
        window = fixture_event_window(self.ctx.settings.fixtures_dir) if self.ctx.http.is_fixture else None
        if window is not None and start < window[0]:
            start = window[0]  # offline: nothing was recorded before the fixture window
        report = IngestReport(as_of, start)
        for src in sources or list(SOURCES):
            try:
                if src == "sec":
                    stats = self.ingest_sec(tickers or list(DEFAULT_TICKERS), as_of=as_of, start=start)
                elif src == "fomc":
                    stats = self.ingest_fomc(as_of=as_of, start=start)
                elif src == "macro":
                    stats = self.ingest_macro(as_of=as_of, start=start)
                else:
                    raise FinanceAgentError(f"unknown source {src!r}")
                report.per_source[src] = stats.__dict__
            except FinanceAgentError as exc:
                msg = f"{src}: {type(exc).__name__}: {exc}"
                log.warning("ingestion source failed", extra={"source": src, "error": str(exc)})
                report.errors.append(msg)
                report.per_source[src] = {"error": msg}
        return report

    # ------------------------------------------------------------ SEC
    def ingest_sec(self, tickers: list[str], *, as_of: datetime | None, start: date) -> IngestStats:
        self.ctx._require_sec_identity()
        total = IngestStats()
        for t in tickers:
            entry = self.ctx.sec.resolve_ticker(t)
            payload, ref = self.ctx.sec.submissions_raw(entry.cik)
            filings = [
                f
                for f in parse_submission_filings(payload)
                if (f.accepted_at or ensure_aware(datetime.combine(f.filing_date, datetime.min.time())))
                .astimezone(ET)
                .date()
                >= start
            ]
            builder = SecEventBuilder(entry.ticker, payload.get("name", entry.title))
            items = builder.filing_events(filings, ref, as_of=as_of)
            total.merge(self._store(items, as_of))
            by_accession = {ev.source_document_id: ev for ev, _ in items}
            periodic = [
                (f, by_accession[f.accession])
                for f in filings
                if f.form in {"10-Q", "10-K"} and f.accession in by_accession
            ]
            if periodic:
                data = self.ctx.company_data(entry.ticker)
                metric_items = []
                for f, fev in periodic:
                    fin = self.ctx.build_financials(data, quarters=2, as_of=available_at(f.filing_date))
                    mev = builder.metric_change_event(f, fin, compute_quarterly_metrics(fin), fev)
                    if mev is not None:
                        metric_items.append(
                            (
                                mev,
                                [
                                    SourceLink(
                                        f"sec_companyfacts:{f.accession}",
                                        "SEC EDGAR XBRL",
                                        "primary",
                                        fev.source_url,
                                    )
                                ],
                            )
                        )
                total.merge(self._store(metric_items, as_of))
        return total

    # ------------------------------------------------------------ FOMC
    def ingest_fomc(self, *, as_of: datetime | None, start: date) -> IngestStats:
        cal_bytes, cal_ref = self.official.get_bytes(
            source_key="fed:fomccalendars", kind="fomc_calendar", url=fomc.CALENDAR_URL, ttl=self.calendar_ttl
        )
        cal_ref = self._observed(cal_ref, "fed/")
        meetings = fomc.parse_calendar(cal_bytes.decode("utf-8", errors="replace"))
        try:
            rss_bytes, _ = self.official.get_bytes(
                source_key="fed:press_monetary_rss", kind="fed_rss", url=fomc.RSS_URL, ttl=self.calendar_ttl
            )
            rss = fomc.parse_rss(rss_bytes)
        except FinanceAgentError as exc:
            log.warning("Fed RSS unavailable; using statement headers for times", extra={"error": str(exc)})
            rss = {}
        b = fomc.FomcEventBuilder()
        items = [b.meeting_event(m, cal_ref) for m in meetings if m.end >= start]
        with_stmt = [m for m in meetings if m.statement_url]
        idx = next((i for i, m in enumerate(with_stmt) if m.end >= start), len(with_stmt))
        prev = None
        for m in with_stmt[max(0, idx - 1) :]:  # one earlier statement provides the comparison base
            doc, ref = self.official.get_bytes(
                source_key=f"fed:{m.statement_url}",
                kind="fomc_statement",
                url=m.statement_url,
                ttl=timedelta(days=3650),
            )
            dec = fomc.parse_statement(doc.decode("utf-8", errors="replace"), m.statement_url)
            if m.end >= start:
                items.append(b.decision_event(dec, prev, ref, rss.get(m.statement_url), m))
            if dec.upper is not None:
                prev = dec  # only statements that state a target range are a comparison base
        return self._store(items, as_of)

    # ------------------------------------------------------------ macro
    def ingest_macro(self, *, as_of: datetime | None, start: date) -> IngestStats:
        check = min((as_of or utcnow()).astimezone(ET).date(), datetime.now(ET).date())
        window = fixture_event_window(self.ctx.settings.fixtures_dir) if self.ctx.http.is_fixture else None
        if window is not None:
            # Offline replay must probe exactly the vintages that were recorded; the requested
            # as_of still filters what is stored (see _store).
            start, check = window[0], window[1].astimezone(ET).date()
        builder = MacroEventBuilder(
            self.alfred, use_scheduled_time=self.ctx.settings.macro_use_confirmed_scheduled_time
        )
        items: list[tuple[FinancialEvent, list[SourceLink]]] = []
        horizon = check + timedelta(days=45)
        ics, ics_ref = self.official.get_bytes(
            source_key="bls:ics", kind="bls_calendar", url=calendars.BLS_ICS_URL, ttl=self.calendar_ttl
        )
        ics_ref = self._observed(ics_ref, "bls/")
        bls = [
            s
            for s in calendars.parse_bls_ics(ics.decode("utf-8", errors="replace"))
            if start <= s.scheduled_for.astimezone(ET).date() <= horizon
        ]
        try:
            bea_doc, bea_ref = self.official.get_bytes(
                source_key="bea:schedule",
                kind="bea_calendar",
                url=calendars.BEA_SCHEDULE_URL,
                ttl=self.calendar_ttl,
            )
            bea_ref = self._observed(bea_ref, "bea/")
            bea = [
                s
                for s in calendars.parse_bea_schedule(bea_doc.decode("utf-8", errors="replace"))
                if start <= s.scheduled_for.astimezone(ET).date() <= horizon
            ]
        except FinanceAgentError as exc:
            log.warning("BEA schedule unavailable", extra={"error": str(exc)})
            bea, bea_ref = [], None
        for s in bls:
            items.append(builder.scheduled_event(s, ics_ref))
        for s in bea:
            items.append(builder.scheduled_event(s, bea_ref))
        releases = []
        unconfirmed = []
        for s in bls + bea:
            if s.scheduled_for.astimezone(ET).date() <= check:
                rel = builder.confirm_scheduled(s, check)
                (releases.append(rel) if rel else unconfirmed.append(s))
        confirmed_dates = {(r.group, r.release_date) for r in releases}
        for group in ("BEA_PIO", "BEA_GDP"):  # historical BEA dates are not on the public schedule page
            for rel in builder.discover(group, start, check):
                if (group, rel.release_date) not in confirmed_dates:
                    releases.append(rel)
        for rel in sorted(releases, key=lambda r: r.release_date):
            items.extend(builder.release_events(rel))
        stats = self._store(items, as_of)
        for s in unconfirmed:  # scheduled date passed, but no source confirmation
            ev, _ = builder.scheduled_event(s, ics_ref)
            stored = self.store.get(ev.event_id)
            if stored and stored.verification_status != "UNCONFIRMED":
                stored.verification_status = "UNCONFIRMED"
                stored.attributes = {
                    **stored.attributes,
                    "unconfirmed_check_date": check.isoformat(),
                    "note": "Scheduled date passed; publication not found in ALFRED vintage.",
                }
                self.store.upsert(stored)
        return stats
