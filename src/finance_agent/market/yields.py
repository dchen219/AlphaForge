"""Treasury constant-maturity yields (DGS2, DGS10) from keyless ALFRED vintages.

Yields are interest rates in percent per annum, not prices. Changes are reported in basis
points (1 percentage point = 100 bp). Daily H.15 observations are end-of-day market quotes:
they do not measure an immediate reaction to an intraday announcement.

Availability (point-in-time):
* An observation for day t is first published in the ALFRED vintage of the next H.15
  publication day (federal-government schedule, incl. Saturday holidays observed on Friday and
  executive-order closures); holiday rows appear one publication day later still. The rule is
  checked against recorded real consecutive-day vintages around holidays (see tests): it is
  never earlier than the real first vintage. ALFRED vintages are dated, not timed, so the observation is treated as
  available from 00:00 America/New_York on the day *after* that first vintage
  (RULE_BASED_CONSERVATIVE). If the value is already present in an earlier vintage that we
  hold, that earlier vintage date is used instead.
* A later vintage that changes a value creates a new version, available from the day after
  the vintage in which it was observed. Earlier values are kept.
* Backfill caveat: the first ingestion reads one (recent) vintage; values revised before that
  vintage cannot be recovered from it and are assumed to equal the first publication.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from finance_agent.events.sources.macro import ALFRED_CSV_URL, parse_alfred_csv
from finance_agent.market.calculations import yield_changes_bp
from finance_agent.market.ingest import fixture_observed
from finance_agent.market.schemas import AvailabilityPrecision
from finance_agent.market.store import MarketStore
from finance_agent.market.universe import YIELD_SERIES
from finance_agent.providers.base import CachedFetcher
from finance_agent.timeutil import ET, available_at, utcnow

UNITS = "percent"


# --------------------------------------------------------------------------- Fed business days


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    d = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


# Federal executive-branch closures by executive order (H.15/FRED did not publish). 2025 dates are
# evidenced by the recorded ALFRED vintages (no new observation in the 12-24 and 12-26 vintages);
# 2024-12-24 and 2025-01-09 are included conservatively (an extra closure can only delay, never
# advance, availability).
AD_HOC_FEDERAL_CLOSURES = frozenset(
    {date(2024, 12, 24), date(2025, 1, 9), date(2025, 12, 24), date(2025, 12, 26)}
)


def federal_holidays_observed(year: int) -> set[date]:
    """US federal holidays as observed by the federal government (OPM): a holiday on Saturday is
    observed the preceding Friday, on Sunday the following Monday. H.15 publication follows this
    schedule (verified: Independence Day 2026 fell on Saturday and nothing was published Friday
    2026-07-03)."""
    fixed = [date(year, 1, 1), date(year, 7, 4), date(year, 11, 11), date(year, 12, 25)]
    if year >= 2021:
        fixed.append(date(year, 6, 19))  # Juneteenth
    out = set()
    for d in fixed:
        out.add(
            d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d
        )
    out |= {
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _last_weekday(year, 5, 0),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 10, 0, 2),  # Columbus Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    }
    # New Year's Day on Saturday is observed on Friday Dec 31 of the previous year.
    if date(year + 1, 1, 1).weekday() == 5:
        out.add(date(year, 12, 31))
    return out


def is_publication_day(d: date) -> bool:
    """A day on which the H.15 / FRED daily update is expected to be published."""
    return d.weekday() < 5 and d not in federal_holidays_observed(d.year) and d not in AD_HOC_FEDERAL_CLOSURES


def next_publication_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_publication_day(d):
        d += timedelta(days=1)
    return d


def first_vintage_rule(observation: date) -> date:
    """Conservative first ALFRED vintage containing ``observation``.

    A publication-day observation appears in the next publication day's vintage. A row for a
    non-publication day (a holiday, published as missing) only appears together with the next
    observation, i.e. one publication day later still. Validated against recorded real
    consecutive-day vintages (never earlier than the actual first vintage).
    """
    nxt = next_publication_day(observation)
    return nxt if is_publication_day(observation) else next_publication_day(nxt)


# --------------------------------------------------------------------------- ingestion


def yield_window(fixtures_dir) -> tuple[date, date] | None:
    import json
    from pathlib import Path

    p = Path(fixtures_dir) / "MANIFEST.json"
    if not p.exists():
        return None
    w = json.loads(p.read_text(encoding="utf-8")).get("yield_window")
    return (date.fromisoformat(w["start"]), date.fromisoformat(w["vintage"])) if w else None


class YieldIngestor:
    def __init__(self, ctx, store: MarketStore | None = None) -> None:
        self.ctx = ctx
        self.store = store or MarketStore(ctx.db)
        self.fetcher = CachedFetcher("official", ctx.http, ctx.snapshots)
        s = ctx.settings
        self.ttl_past = timedelta(hours=s.cache_ttl_alfred_past_hours)
        self.ttl_today = timedelta(hours=s.cache_ttl_alfred_today_hours)

    def vintage(self, series_id: str, vintage: date, start: date, end: date):
        today = datetime.now(ET).date()
        content, ref = self.fetcher.get_bytes(
            source_key=f"alfred:{series_id}:{vintage.isoformat()}:{start}:{end}",
            kind="alfred_vintage_csv",
            url=ALFRED_CSV_URL,
            params={
                "id": series_id,
                "vintage_date": vintage.isoformat(),
                "cosd": start.isoformat(),
                "coed": end.isoformat(),
            },
            ttl=self.ttl_today if vintage >= today else self.ttl_past,
            description=f"ALFRED {series_id} vintage {vintage}",
        )
        if self.ctx.http.is_fixture:
            ref = fixture_observed(ref, self.ctx.settings.fixtures_dir, "alfred/")
        return parse_alfred_csv(content, series_id), ref

    def run(self, series: list[str] | None, start: date, end: date, *, as_of: datetime | None = None) -> dict:
        series = [s.upper() for s in (series or list(YIELD_SERIES))]
        unknown = [s for s in series if s not in YIELD_SERIES]
        if unknown:
            raise ValueError(f"unknown yield series {unknown}; supported: {list(YIELD_SERIES)}")
        vintage = min((as_of or utcnow()).astimezone(ET).date(), datetime.now(ET).date())
        if self.ctx.http.is_fixture:
            window = yield_window(self.ctx.settings.fixtures_dir)
            if window:
                start, vintage = max(start, window[0]), window[1]
        stats = {
            "series": series,
            "vintage": vintage.isoformat(),
            "inserted": 0,
            "unchanged": 0,
            "revisions": 0,
            "missing": 0,
        }
        for sid in series:
            data, ref = self.vintage(sid, vintage, start, end)
            for obs, value in sorted(data.items()):
                if not (start <= obs <= end) or obs >= vintage:
                    continue  # an observation for the vintage date itself is not yet published
                first = min(first_vintage_rule(obs), vintage)
                outcome = self.store.insert_yield(
                    {
                        "series_id": sid,
                        "observation_date": obs,
                        "value": value,
                        "units": UNITS,
                        "vintage_date": vintage,
                        "first_vintage_rule": first,
                        "available_at": available_at(first),
                        "revision_available_at": available_at(vintage),
                        "availability_precision": AvailabilityPrecision.RULE_BASED_CONSERVATIVE,
                        "snapshot_id": ref.snapshot_id,
                        "snapshot_sha256": ref.sha256,
                        "data_origin": ref.origin,
                        "retrieved_at": ref.retrieved_at,
                    }
                )
                stats["missing"] += value is None
                stats[
                    {"inserted": "inserted", "unchanged": "unchanged", "revision": "revisions"}[outcome]
                ] += 1
        return stats


def yield_series(store: MarketStore, series_id: str, start: date, end: date, as_of: datetime | None) -> dict:
    rows = store.yields(series_id, start, end, as_of)
    changes = yield_changes_bp([(r["observation_date"], r["value"]) for r in rows])
    return {
        "series_id": series_id,
        "title": YIELD_SERIES.get(series_id),
        "units": f"{UNITS} per annum (changes in basis points; 1 percentage point = 100 bp)",
        "as_of": as_of,
        "observations": rows,
        "changes": changes,
        "notes": [
            "Treasury yields are interest rates, not ETF prices.",
            "Daily end-of-day H.15 observations; not an intraday post-announcement reaction measure.",
            "Missing observations (holidays / not published) are reported as null, never filled.",
        ],
    }
