"""Confounding-event audit using AlphaForge's own event store (v0.2).

For a study window [lo, hi] (session offsets around day 0), every OTHER recorded, occurred event
is aligned with the same daily rules; it is a confounder when its day 0 falls inside the window
and it is relevant to the target:

* MACRO_RELEASE / CENTRAL_BANK: market-wide (relevant to every equity, ETF and yield study);
* corporate filings, earnings and other disclosures: only when they name the target ticker;
* FINANCIAL_METRIC_CHANGE events are derived from a filing (``relation = derived``), and events
  released together with the study event (same source, same instant/date) are part of the SAME
  announcement (``relation = same_announcement``), not independent confounders.

Absence of confounders is only meaningful inside the recorded coverage of each source family.
AlphaForge stores no ingestion-window record for events, so coverage is INFERRED from the range
of stored events of that family; anything outside it is reported as COVERAGE_INCOMPLETE. The
importance score is a heuristic ranking, not a causal-impact measure. Point-in-time studies
only see events available at the cutoff.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from finance_agent.event_studies.alignment import align_event
from finance_agent.event_studies.schemas import ConfoundingEvent, EventAlignment
from finance_agent.events.schemas import EventCategory, EventSubtype, FinancialEvent, RevisionStatus
from finance_agent.events.store import EventStore
from finance_agent.market.calendar import TradingCalendar
from finance_agent.timeutil import ET

AVAILABILITY_MARGIN_DAYS = 14
MARKET_WIDE = {EventCategory.MACRO_RELEASE, EventCategory.CENTRAL_BANK}
FAMILIES = {
    "macro (BLS/BEA via ALFRED)": ("BLS via ALFRED", "BEA via ALFRED"),
    "central bank (Federal Reserve)": ("Federal Reserve",),
}
NOT_COVERED = [
    "news, unscheduled announcements and analyst actions",
    "disclosures by companies other than the target (and by benchmark constituents)",
    "macro releases AlphaForge does not ingest (e.g. PPI, retail sales, ISM, jobless claims)",
    "non-U.S. events and Treasury auctions",
]


def _same_announcement(a: FinancialEvent, b: FinancialEvent, aa: EventAlignment, ab: EventAlignment) -> bool:
    if a.source_name != b.source_name or a.event_type != b.event_type or aa.day0 != ab.day0:
        return False
    if aa.event_time is not None and ab.event_time is not None:
        return abs((aa.event_time - ab.event_time).total_seconds()) <= 60
    return aa.local_date == ab.local_date


def _coverage(store: EventStore, target: str, is_equity: bool, lo: date, hi: date, as_of) -> dict:
    fams = dict(FAMILIES)
    out = {}
    for name, sources in fams.items():
        evs = [e for s in sources for e in store.query(source=s, as_of=as_of, limit=10000)]
        out[name] = _range(evs, lo, hi)
    if is_equity:
        evs = store.query(source="SEC EDGAR", ticker=target, as_of=as_of, limit=10000)
        out[f"SEC EDGAR filings for {target}"] = _range(evs, lo, hi)
    return out


def _range(evs: list[FinancialEvent], lo: date, hi: date) -> dict:
    if not evs:
        return {"status": "NOT_COVERED", "reason": "no stored events from this source family"}
    first = min(e.available_at for e in evs).astimezone(ET).date()
    last = max(e.available_at for e in evs).astimezone(ET).date()
    covered = first <= lo and last >= hi
    return {
        "status": "COVERED" if covered else "NOT_COVERED",
        "stored_range": [str(first), str(last)],
        "basis": "inferred from the range of stored events (no ingestion-window record exists)",
    }


def detect(
    store: EventStore,
    calendar: TradingCalendar,
    study_event: FinancialEvent,
    study_alignment: EventAlignment,
    grid: dict[int, date],
    lo: int,
    hi: int,
    *,
    target: str,
    is_equity: bool,
    as_of: datetime | None,
    day0_fn=None,
) -> dict:
    """``day0_fn(event, alignment) -> date | None`` overrides the equity day-0 rule (yield studies)."""
    by_date = {d: k for k, d in grid.items()}
    d_lo, d_hi = grid[lo], grid[hi]
    # candidates are selected by availability; DATE_ONLY events become available the next day, so a
    # 14-day margin captures events published in the window but made available shortly afterwards
    start = datetime.combine(d_lo - timedelta(days=AVAILABILITY_MARGIN_DAYS), datetime.min.time(), tzinfo=ET)
    end = datetime.combine(d_hi + timedelta(days=AVAILABILITY_MARGIN_DAYS), datetime.max.time(), tzinfo=ET)
    candidates = store.query(start=start, end=end, as_of=as_of, limit=2000)
    found, same, derived = [], [], []
    for e in candidates:
        if e.event_id == study_event.event_id:
            continue
        cat = EventCategory(e.event_type)
        market_wide = cat in MARKET_WIDE
        if not market_wide and (not is_equity or target.upper() not in e.affected_entities):
            continue
        al = align_event(e, calendar)
        day0 = day0_fn(e, al) if day0_fn else al.day0
        if day0 is None or day0 not in by_date:
            continue
        k = by_date[day0]
        if not lo <= k <= hi:
            continue
        relation, reason = (
            "confounder",
            (
                "market-wide macro/policy announcement in the window"
                if market_wide
                else f"{target} disclosure in the window"
            ),
        )
        if e.event_subtype == EventSubtype.FINANCIAL_METRIC_CHANGE:
            relation, reason = (
                "derived",
                "metric-change event derived from a filing (not independent information)",
            )
        elif _same_announcement(study_event, e, study_alignment, al) or (
            e.revision_status == RevisionStatus.REVISION and al.day0 == study_alignment.day0
            and e.source_name == study_event.source_name
        ):  # fmt: skip
            relation, reason = (
                "same_announcement",
                "released together with the study event (same announcement)",
            )
        c = ConfoundingEvent(
            event_id=e.event_id, event_type=str(e.event_type), event_subtype=str(e.event_subtype),
            title=e.title, published_at=al.event_time, time_basis=al.time_basis,
            affected_entities=list(e.affected_entities), relative_session=k,
            importance_score=e.importance_score, source_name=e.source_name,
            source_url=e.source_url, reason=reason, relation=relation,
        )  # fmt: skip
        {"confounder": found, "same_announcement": same, "derived": derived}[relation].append(c)
    coverage = _coverage(store, target, is_equity, d_lo, d_hi, as_of)
    incomplete = [n for n, c in coverage.items() if c["status"] != "COVERED"]
    if found:
        status = "CONFOUNDERS_PRESENT"
    elif incomplete:
        status = "COVERAGE_INCOMPLETE"
    else:
        status = "NONE_FOUND_WITHIN_COVERAGE"
    key = lambda c: (c.relative_session, c.event_id)  # noqa: E731
    return {
        "status": status,
        "search_window": {"offsets": [lo, hi], "sessions": [str(d_lo), str(d_hi)]},
        "point_in_time_as_of": as_of,
        "confounders": [c.model_dump(mode="json") for c in sorted(found, key=key)],
        "same_announcement": [c.model_dump(mode="json") for c in sorted(same, key=key)],
        "derived": [c.model_dump(mode="json") for c in sorted(derived, key=key)],
        "coverage": coverage,
        "incomplete_families": incomplete,
        "not_covered_by_any_source": NOT_COVERED,
        "statement": (
            "Absence of confounders is verified only for the covered source families listed; it is "
            "not evidence that nothing else happened. Importance scores are heuristic rankings, "
            "not causal-impact measures."
        ),
    }
