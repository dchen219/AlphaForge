"""Macro release detection from official calendars + ALFRED vintages (no API key needed).

Method
1. Calendars (BLS iCalendar, BEA schedule) give *scheduled* releases.
2. ALFRED keyless vintage CSVs (``alfredgraph.csv?id=SERIES&vintage_date=D``) give the
   data exactly as known on day D. A release on D is CONFIRMED when an observation is
   present in vintage D but absent from vintage D−1. Values changed between D−1 and D are
   revisions.
3. Without a calendar date (historical BEA releases), the publication date is found by
   bisection over vintages between two probes.

The first-release value is stored in its own event and never overwritten; a revision is a
new event that supersedes the previous version.
"""

from __future__ import annotations

import csv
import io
import itertools
import logging
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from finance_agent.errors import MalformedDataError, NotFoundError
from finance_agent.events.comparisons import (
    IndicatorSpec,
    compute_comparisons,
    headline_series,
    revision_comparison,
    shift_months,
)
from finance_agent.events.schemas import (
    AvailabilityPrecision,
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    RevisionStatus,
    Unit,
    VerificationStatus,
    make_event_id,
)
from finance_agent.events.sources.calendars import RELEASE_GROUPS, ScheduledRelease
from finance_agent.events.store import SourceLink
from finance_agent.providers.base import CachedFetcher
from finance_agent.storage.snapshots import SnapshotRef
from finance_agent.timeutil import ET, available_at, utcnow

log = logging.getLogger(__name__)
ALFRED_CSV_URL = "https://alfred.stlouisfed.org/graph/alfredgraph.csv"
MAX_REVISION_EVENTS = 3  # recent prior periods turned into revision events per release


@dataclass(frozen=True)
class Indicator:
    series_id: str
    subtype: EventSubtype
    name: str
    group: str
    spec: IndicatorSpec
    industries: tuple[str, ...]


def _spec(sid, measure, hu, lu, freq, yoy):
    return IndicatorSpec(sid, measure, hu, lu, freq, yoy)


INDICATORS: dict[str, Indicator] = {
    i.series_id: i
    for i in [
        Indicator(
            "CPIAUCSL",
            EventSubtype.CPI,
            "Consumer Price Index (all items, SA)",
            "BLS_CPI",
            _spec("CPIAUCSL", "pct_change", Unit.PERCENT_CHANGE, Unit.INDEX_POINTS, "M", "pct"),
            ("rates", "consumer_discretionary"),
        ),
        Indicator(
            "CPILFESL",
            EventSubtype.CORE_CPI,
            "Core CPI (less food and energy, SA)",
            "BLS_CPI",
            _spec("CPILFESL", "pct_change", Unit.PERCENT_CHANGE, Unit.INDEX_POINTS, "M", "pct"),
            ("rates",),
        ),
        Indicator(
            "PAYEMS",
            EventSubtype.NONFARM_PAYROLLS,
            "Total nonfarm payroll employment",
            "BLS_EMPSIT",
            _spec("PAYEMS", "change", Unit.THOUSANDS_OF_PERSONS, Unit.THOUSANDS_OF_PERSONS, "M", "pct"),
            ("rates", "cyclicals"),
        ),
        Indicator(
            "UNRATE",
            EventSubtype.UNEMPLOYMENT_RATE,
            "Unemployment rate",
            "BLS_EMPSIT",
            _spec("UNRATE", "level", Unit.PERCENT, Unit.PERCENT, "M", "diff"),
            ("rates", "cyclicals"),
        ),
        Indicator(
            "PCEPI",
            EventSubtype.PCE_PRICE_INDEX,
            "PCE price index",
            "BEA_PIO",
            _spec("PCEPI", "pct_change", Unit.PERCENT_CHANGE, Unit.INDEX_POINTS, "M", "pct"),
            ("rates",),
        ),
        Indicator(
            "PCEPILFE",
            EventSubtype.CORE_PCE_PRICE_INDEX,
            "Core PCE price index",
            "BEA_PIO",
            _spec("PCEPILFE", "pct_change", Unit.PERCENT_CHANGE, Unit.INDEX_POINTS, "M", "pct"),
            ("rates",),
        ),
        Indicator(
            "GDPC1",
            EventSubtype.REAL_GDP,
            "Real GDP (chained 2017 dollars, SAAR)",
            "BEA_GDP",
            _spec("GDPC1", "annualized_pct_change", Unit.PERCENT_CHANGE, Unit.BILLIONS_USD, "Q", "pct"),
            ("cyclicals", "rates"),
        ),
    ]
}
GROUP_SERIES = {g: [i.series_id for i in INDICATORS.values() if i.group == g] for g in RELEASE_GROUPS}


def parse_alfred_csv(content: bytes, series_id: str) -> dict[date, float | None]:
    text = content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text)))
    if (
        not rows
        or len(rows[0]) < 2
        or rows[0][0] != "observation_date"
        or not rows[0][1].upper().startswith(series_id)
    ):
        raise MalformedDataError(
            f"ALFRED CSV for {series_id} has an unexpected header: {rows[0] if rows else None}"
        )
    out: dict[date, float | None] = {}
    for r in rows[1:]:
        if len(r) < 2 or not r[0]:
            continue
        try:
            d = date.fromisoformat(r[0])
        except ValueError as exc:
            raise MalformedDataError(f"bad ALFRED date {r[0]!r}") from exc
        v = r[1].strip()
        try:
            out[d] = None if v in {".", ""} else float(v)
        except ValueError as exc:
            raise MalformedDataError(f"bad ALFRED value {v!r}") from exc
    return out


def period_label(d: date, frequency: str) -> str:
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}" if frequency == "Q" else f"{d.year}-{d.month:02d}"


def period_text(d: date, frequency: str) -> str:
    return f"Q{(d.month - 1) // 3 + 1} {d.year}" if frequency == "Q" else d.strftime("%B %Y")


class AlfredClient:
    def __init__(self, fetcher: CachedFetcher, *, ttl_past: timedelta, ttl_today: timedelta) -> None:
        self.fetcher = fetcher
        self.ttl_past = ttl_past
        self.ttl_today = ttl_today
        self.requests = 0
        self._memo: dict[tuple[str, date], tuple[dict[date, float | None], SnapshotRef]] = {}

    def vintage(self, series_id: str, d: date) -> tuple[dict[date, float | None], SnapshotRef]:
        key = (series_id, d)
        if key in self._memo:
            return self._memo[key]
        today = datetime.now(ET).date()
        if d > today:
            raise NotFoundError(f"ALFRED vintage {d} is in the future")
        self.requests += 1
        content, ref = self.fetcher.get_bytes(
            source_key=f"alfred:{series_id}:{d.isoformat()}",
            kind="alfred_vintage_csv",
            url=ALFRED_CSV_URL,
            params={"id": series_id, "vintage_date": d.isoformat()},
            ttl=self.ttl_today if d >= today else self.ttl_past,
            description=f"ALFRED {series_id} vintage {d}",
        )
        out = (parse_alfred_csv(content, series_id), ref)
        self._memo[key] = out
        return out

    def first_vintage_with(
        self, series_id: str, obs: date, lo: date, hi: date, baseline: float | None
    ) -> date | None:
        """Smallest D in (lo, hi] whose vintage differs from ``baseline`` for ``obs``.

        ``baseline`` None means 'observation absent'. Requires monotone change in (lo, hi].
        """

        def changed(d: date) -> bool:
            v = self.vintage(series_id, d)[0].get(obs)
            return (v is not None) if baseline is None else (v is not None and abs(v - baseline) > 1e-9)

        if not changed(hi):
            return None
        while (hi - lo).days > 1:
            mid = lo + (hi - lo) // 2
            if changed(mid):
                hi = mid
            else:
                lo = mid
        return hi


@dataclass
class ConfirmedRelease:
    group: str
    release_date: date
    scheduled: ScheduledRelease | None
    discovered_by: str  # calendar_confirmed | bisection


def _diff(prev: dict[date, float | None], cur: dict[date, float | None]):
    new = sorted(d for d, v in cur.items() if v is not None and prev.get(d) is None)
    revised = sorted(
        d for d, v in cur.items() if v is not None and prev.get(d) is not None and abs(v - prev[d]) > 1e-9
    )
    return new, revised


class MacroEventBuilder:
    def __init__(self, alfred: AlfredClient, *, use_scheduled_time: bool = True) -> None:
        self.alfred = alfred
        self.use_scheduled_time = use_scheduled_time

    # ------------------------------------------------------------ scheduled
    def scheduled_event(
        self, s: ScheduledRelease, ref: SnapshotRef
    ) -> tuple[FinancialEvent, list[SourceLink]]:
        g = RELEASE_GROUPS[s.group]
        key = f"SCHED|{s.group}|{s.scheduled_for.astimezone(ET).date().isoformat()}"
        ev = FinancialEvent(
            canonical_event_key=key,
            event_type=EventCategory.MACRO_RELEASE,
            event_subtype=EventSubtype.RELEASE_SCHEDULED,
            event_status=EventStatus.SCHEDULED,
            title=f"Scheduled: {s.release_name} ({g['agency']})",
            description="Calendar entry only; not evidence that the release occurred.",
            source_name=f"{g['agency']} release calendar",
            source_url=ref.url,
            source_document_id=s.source_uid,
            source_snapshot_hash=ref.sha256,
            data_origin=ref.origin,
            affected_entities=GROUP_SERIES.get(s.group, []),
            scheduled_for=s.scheduled_for,
            availability_precision=AvailabilityPrecision.UNKNOWN,
            verification_status=VerificationStatus.SCHEDULE_ONLY,
            attributes={
                "group": s.group,
                "release_name": s.release_name,
                "scheduled_time_known": s.time_known,
            },
        )
        return ev, [
            SourceLink(
                f"schedule:{ref.sha256}:{s.source_uid}",
                ev.source_name,
                "schedule",
                ref.url,
                ref.snapshot_id,
                ref.sha256,
                ref.retrieved_at,
            )
        ]

    # ------------------------------------------------------------ confirmation
    def confirm_scheduled(self, s: ScheduledRelease, check_date: date) -> ConfirmedRelease | None:
        """Confirm a scheduled release with ALFRED (D−1 vs D) for the group's primary series."""
        d = s.scheduled_for.astimezone(ET).date()
        if d > check_date:
            return None
        primary = GROUP_SERIES[s.group][0]
        prev, _ = self.alfred.vintage(primary, d - timedelta(days=1))
        cur, _ = self.alfred.vintage(primary, d)
        new, revised = _diff(prev, cur)
        if new or revised:
            return ConfirmedRelease(s.group, d, s, "calendar_confirmed")
        return None

    def discover(self, group: str, start: date, end: date) -> list[ConfirmedRelease]:
        """Bisection-based discovery of release dates in (start, end] (no calendar needed)."""
        primary = GROUP_SERIES[group][0]
        found: set[date] = set()
        probes = [start]
        month_end = shift_months(start.replace(day=1), 1) - timedelta(days=1)
        while month_end < end:
            if month_end > start:
                probes.append(month_end)
            month_end = shift_months(month_end.replace(day=1), 2) - timedelta(days=1)
        probes.append(end)
        for lo, hi in itertools.pairwise(probes):
            a, _ = self.alfred.vintage(primary, lo)
            b, _ = self.alfred.vintage(primary, hi)
            new, revised = _diff(a, b)
            for obs in new:
                d = self.alfred.first_vintage_with(primary, obs, lo, hi, None)
                if d:
                    found.add(d)
            for obs in revised[-1:]:  # latest revised obs locates revision-only releases (e.g. GDP 2nd/3rd)
                d = self.alfred.first_vintage_with(primary, obs, lo, hi, a.get(obs))
                if d:
                    found.add(d)
        return [ConfirmedRelease(group, d, None, "bisection") for d in sorted(found)]

    # ------------------------------------------------------------ events
    def release_events(self, rel: ConfirmedRelease) -> list[tuple[FinancialEvent, list[SourceLink]]]:
        out: list[tuple[FinancialEvent, list[SourceLink]]] = []
        d = rel.release_date
        g = RELEASE_GROUPS[rel.group]
        cluster = f"CL-{rel.group}-{d.isoformat()}"
        sched_id = make_event_id(f"SCHED|{rel.group}|{d.isoformat()}") if rel.scheduled else None
        if rel.scheduled and rel.scheduled.time_known and self.use_scheduled_time:
            avail, precision = (
                rel.scheduled.scheduled_for,
                AvailabilityPrecision.SCHEDULED_TIME_CONFIRMED_DATE,
            )
        else:
            avail, precision = available_at(d), AvailabilityPrecision.DATE_ONLY_CONSERVATIVE
        for sid in GROUP_SERIES[rel.group]:
            ind = INDICATORS[sid]
            prev, pref = self.alfred.vintage(sid, d - timedelta(days=1))
            cur, cref = self.alfred.vintage(sid, d)
            new, revised = _diff(prev, cur)
            links = [
                SourceLink(
                    f"alfred:{sid}:{d}",
                    "ALFRED (St. Louis Fed)",
                    "vintage",
                    cref.url,
                    cref.snapshot_id,
                    cref.sha256,
                    cref.retrieved_at,
                ),
                SourceLink(
                    f"alfred:{sid}:{d - timedelta(days=1)}",
                    "ALFRED (St. Louis Fed)",
                    "vintage_prior",
                    pref.url,
                    pref.snapshot_id,
                    pref.sha256,
                    pref.retrieved_at,
                ),
            ]
            if rel.scheduled:
                links.append(
                    SourceLink(
                        f"schedule:{rel.scheduled.source_uid}", f"{g['agency']} release calendar", "schedule"
                    )
                )
            common = dict(
                event_type=EventCategory.MACRO_RELEASE,
                event_subtype=ind.subtype,
                series_id=sid,
                source_name=f"{g['agency']} via ALFRED",
                source_url=cref.url,
                source_snapshot_hash=cref.sha256,
                data_origin=cref.origin,
                affected_entities=[sid],
                affected_industries=list(ind.industries),
                available_at=avail,
                availability_precision=precision,
                scheduled_for=rel.scheduled.scheduled_for if rel.scheduled else None,
                verification_status=VerificationStatus.SOURCE_VERIFIED,
                event_cluster_id=cluster,
                related_event_id=sched_id,
                ingested_at=utcnow(),
            )
            for obs in new:
                comps = compute_comparisons(ind.spec, cur, obs, d.isoformat())
                heads = headline_series(cur, ind.spec.measure, ind.spec.frequency)
                head = heads.get(obs)
                prev_head = heads.get(shift_months(obs, -(3 if ind.spec.frequency == "Q" else 1)))
                ev = FinancialEvent(
                    canonical_event_key=f"MACRO|{sid}|{period_label(obs, ind.spec.frequency)}|FIRST_RELEASE",
                    event_status=EventStatus.PUBLISHED,
                    revision_status=RevisionStatus.FIRST_RELEASE,
                    title=f"{ind.name} — {period_text(obs, ind.spec.frequency)} (first release)",
                    source_document_id=f"ALFRED:{sid}:{d.isoformat()}",
                    observation_period=period_label(obs, ind.spec.frequency),
                    observation_date=obs,
                    value=head,
                    previous_value=prev_head,
                    unit=ind.spec.headline_unit,
                    comparisons=comps,
                    attributes={
                        "release_group": rel.group,
                        "release_name": g["name"],
                        "agency": g["agency"],
                        "publication_date": d.isoformat(),
                        "vintage_date": d.isoformat(),
                        "level": cur[obs],
                        "level_unit": str(ind.spec.level_unit),
                        "headline_measure": ind.spec.measure,
                        "discovered_by": rel.discovered_by,
                        "revised_observations_in_release": len(revised),
                    },
                    **common,
                )
                out.append((ev, links))
            for obs in revised[-MAX_REVISION_EVENTS:]:
                old, new_v = prev[obs], cur[obs]
                comp = revision_comparison(
                    sid,
                    obs,
                    old,
                    new_v,
                    (d - timedelta(days=1)).isoformat(),
                    d.isoformat(),
                    ind.spec.level_unit,
                )
                old_head = headline_series(prev, ind.spec.measure, ind.spec.frequency).get(obs)
                cur_heads = headline_series(cur, ind.spec.measure, ind.spec.frequency)
                new_head = cur_heads.get(obs)
                hist = [v for k, v in sorted(cur_heads.items()) if k < obs][-60:]
                headline_sd = statistics.stdev(hist) if len(hist) >= 24 else None
                period = period_label(obs, ind.spec.frequency)
                prior_key_prefix = f"MACRO|{sid}|{period}|"
                ev = FinancialEvent(
                    canonical_event_key=f"MACRO|{sid}|{period}|REVISION|{d.isoformat()}",
                    event_status=EventStatus.REVISED,
                    revision_status=RevisionStatus.REVISION,
                    title=f"{ind.name} — {period_text(obs, ind.spec.frequency)} revised",
                    source_document_id=f"ALFRED:{sid}:{d.isoformat()}",
                    observation_period=period,
                    observation_date=obs,
                    value=new_head,
                    previous_value=old_head,
                    unit=ind.spec.headline_unit,
                    comparisons=[comp],
                    attributes={
                        "release_group": rel.group,
                        "vintage_date": d.isoformat(),
                        "previous_vintage_date": (d - timedelta(days=1)).isoformat(),
                        "level": new_v,
                        "previous_level": old,
                        "headline_sd": headline_sd,
                        "level_unit": str(ind.spec.level_unit),
                        "supersedes_key_prefix": prior_key_prefix,
                        "discovered_by": rel.discovered_by,
                    },
                    **common,
                )
                out.append((ev, links))
        return out
