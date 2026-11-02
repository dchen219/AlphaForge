"""Federal Reserve FOMC events: meeting calendar, policy statements, rate decisions.

Sources (federalreserve.gov has no robots.txt restrictions for these paths):
  /monetarypolicy/fomccalendars.htm            meeting calendar + statement links
  /feeds/press_monetary.xml                    RSS with exact publication timestamps
  /newsevents/pressreleases/monetaryYYYYMMDDa.htm   statement text

Decision classification is source-grounded: the statement's own sentence
("decided to raise|lower|maintain the target range ... to X to Y percent") is parsed and
cross-checked against the previous verified statement's range. No sentiment scoring.
A scheduled meeting alone is never treated as a decision.
"""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET_XML
from dataclasses import dataclass, field
from datetime import date, datetime, time
from email.utils import parsedate_to_datetime

from finance_agent.errors import MalformedDataError
from finance_agent.events.schemas import (
    AvailabilityPrecision,
    Comparison,
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    RevisionStatus,
    Unit,
    VerificationStatus,
    make_event_id,
)
from finance_agent.events.store import SourceLink
from finance_agent.storage.snapshots import SnapshotRef
from finance_agent.timeutil import ET, available_at, utcnow

FED = "https://www.federalreserve.gov"
CALENDAR_URL = f"{FED}/monetarypolicy/fomccalendars.htm"
RSS_URL = f"{FED}/feeds/press_monetary.xml"
MONTHS = {
    m: i
    for i, m in enumerate(
        [
            "January",
            "February",
            "March",
            "April",
            "May",
            "June",
            "July",
            "August",
            "September",
            "October",
            "November",
            "December",
        ],
        1,
    )
}


@dataclass(frozen=True)
class Meeting:
    start: date
    end: date
    has_projections: bool
    statement_url: str | None
    unscheduled: bool = False


@dataclass
class StatementDecision:
    url: str
    statement_date: date
    release_time: datetime | None  # from "For release at H:MM p.m. EDT/EST"
    action: str | None  # raise | lower | maintain
    lower: float | None
    upper: float | None
    change_text_bp: float | None  # from "by 1/4 percentage point"
    passage: str | None
    vote: str | None
    warnings: list[str] = field(default_factory=list)
    doc_title: str | None = None  # the press release's own title


def _text(doc: str) -> str:
    t = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", doc)
    t = html.unescape(re.sub(r"<[^>]+>", " ", t))
    return re.sub(r"\s+", " ", t).replace("‑", "-").replace("–", "-").replace("—", "-")


def parse_fraction(s: str) -> float:
    """'3-3/4' -> 3.75, '4' -> 4.0, '1/4' -> 0.25, '0' -> 0."""
    s = s.strip()
    m = re.fullmatch(r"(\d+)(?:[- ](\d+)/(\d+))?", s)
    if m:
        whole = float(m.group(1))
        return whole + (float(m.group(2)) / float(m.group(3)) if m.group(2) else 0.0)
    m = re.fullmatch(r"(\d+)/(\d+)", s)
    if m:
        return float(m.group(1)) / float(m.group(2))
    raise ValueError(f"not a Fed fraction: {s!r}")


def parse_calendar(doc: str) -> list[Meeting]:
    if "FOMC Meetings" not in doc:
        raise MalformedDataError("FOMC calendar page structure not recognized")
    meetings: list[Meeting] = []
    heads = [(m.start(), int(m.group(1))) for m in re.finditer(r"(\d{4}) FOMC Meetings", doc)]
    for i, (pos, year) in enumerate(heads):
        block = doc[pos : heads[i + 1][0] if i + 1 < len(heads) else len(doc)]
        rows = re.split(
            r'(?=<div[^>]*class="[^"]*\bfomc-meeting\b[^"]*row)|(?=<div[^>]*class="[^"]*row[^"]*\bfomc-meeting\b)',
            block,
        )
        for row in rows:
            mm = re.search(r"fomc-meeting__month[^>]*>\s*<strong>([^<]+)</strong>", row)
            dd = re.search(r"fomc-meeting__date[^>]*>([^<]+)<", row)
            if not mm or not dd:
                continue
            month_txt, days_txt = mm.group(1).strip(), dd.group(1).strip()
            months = [MONTHS[x] for x in re.split(r"[/-]", month_txt) if x in MONTHS]
            days = [int(x) for x in re.findall(r"\d+", days_txt)]
            if not months or not days:
                continue
            m_start, m_end = months[0], months[-1]
            d_start, d_end = days[0], days[-1]
            try:
                start, end = date(year, m_start, d_start), date(year, m_end, d_end)
            except ValueError:
                continue
            link = re.search(r'href="(/newsevents/pressreleases/monetary\d{8}a\.htm)"', row)
            meetings.append(
                Meeting(
                    start,
                    end,
                    "*" in days_txt,
                    FED + link.group(1) if link else None,
                    "unscheduled" in row.lower(),
                )
            )
    return sorted(set(meetings), key=lambda m: m.end)


def parse_rss(doc: bytes) -> dict[str, datetime]:
    """Statement URL -> exact publication timestamp (RSS pubDate)."""
    try:
        root = ET_XML.fromstring(doc)
    except ET_XML.ParseError as exc:
        raise MalformedDataError(f"Fed RSS is not valid XML: {exc}") from exc
    out = {}
    for it in root.iter("item"):
        title, link, pub = (
            it.findtext("title") or "",
            (it.findtext("link") or "").strip(),
            it.findtext("pubDate"),
        )
        if "FOMC statement" in title and link and pub:
            out[link.replace("http://", "https://")] = parsedate_to_datetime(pub)
    return out


_DECISION = re.compile(
    r"(?:Committee|FOMC)\s+decided\s+to\s+(raise|lower|maintain|reduce|increase)\s+the\s+target\s+range\s+for\s+the\s+"
    r"federal\s+funds\s+rate(?:\s+by\s+(?P<by>[\d/ -]+?)\s+percentage\s+point)?\s+(?:at|to)\s+"
    r"(?P<lo>[\d/ -]+?)\s+to\s+(?P<hi>[\d/ -]+?)\s+percent",
    re.I,
)


def parse_statement(doc: str, url: str) -> StatementDecision:
    text = _text(doc)
    m_date = re.search(r"monetary(\d{8})a\.htm", url)
    if not m_date:
        raise MalformedDataError(f"unexpected statement URL {url}")
    sdate = datetime.strptime(m_date.group(1), "%Y%m%d").date()
    rel = None
    mt = re.search(r"For release at (\d{1,2}):(\d{2})\s*([ap])\.m\.\s*(EDT|EST|ET)", text, re.I)
    if mt:
        hh = int(mt.group(1)) % 12 + (12 if mt.group(3).lower() == "p" else 0)
        rel = datetime.combine(sdate, time(hh, int(mt.group(2))), tzinfo=ET)
    mtitle = re.search(r"(?is)<title>(.*?)</title>", doc)
    title = re.sub(r"\s+", " ", html.unescape(mtitle.group(1))).strip() if mtitle else None
    if title:
        title = re.sub(r"^\s*Federal Reserve Board\s*-\s*", "", title)
    dec = StatementDecision(url, sdate, rel, None, None, None, None, None, None, doc_title=title)
    m = _DECISION.search(text)
    if not m:
        dec.warnings.append("No explicit target-range decision sentence found; decision not classified.")
        return dec
    verb = m.group(1).lower()
    dec.action = {"reduce": "lower", "increase": "raise"}.get(verb, verb)
    try:
        dec.lower, dec.upper = parse_fraction(m.group("lo")), parse_fraction(m.group("hi"))
        if m.group("by"):
            dec.change_text_bp = parse_fraction(m.group("by")) * 100
    except ValueError as exc:
        dec.warnings.append(f"Could not parse range numbers: {exc}")
    start = max(text.rfind(". ", 0, m.start()), text.rfind(": ", 0, m.start())) + 2  # sentence start
    start = start if start >= 2 else m.start()
    end = text.find(".", m.end())
    dec.passage = text[start : end + 1 if end > 0 else m.end()].strip()
    mv = re.search(r"by a (\d+)\s*[-– ]\s*(\d+) vote", text)
    dec.vote = f"{mv.group(1)}-{mv.group(2)}" if mv else None
    return dec


def classify(dec: StatementDecision, prev: StatementDecision | None) -> tuple[EventSubtype, list[str]]:
    """Subtype from the statement's explicit verb, cross-checked with the previous range."""
    notes = list(dec.warnings)
    if dec.action is None or dec.upper is None:
        return EventSubtype.POLICY_STATEMENT, notes
    sub = {
        "raise": EventSubtype.RATE_HIKE,
        "lower": EventSubtype.RATE_CUT,
        "maintain": EventSubtype.RATE_HOLD,
    }[dec.action]
    if prev is not None and prev.upper is not None:
        delta = round((dec.upper - prev.upper) * 100, 6)
        implied = (
            EventSubtype.RATE_HIKE
            if delta > 0
            else EventSubtype.RATE_CUT
            if delta < 0
            else EventSubtype.RATE_HOLD
        )
        if implied != sub:
            notes.append(
                f"Statement verb '{dec.action}' conflicts with range change vs previous statement "
                f"({delta:+.0f} bp); classified as POLICY_STATEMENT."
            )
            return EventSubtype.POLICY_STATEMENT, notes
        if dec.change_text_bp is not None and abs(abs(delta) - dec.change_text_bp) > 1e-6:
            notes.append(
                f"Stated change {dec.change_text_bp:.0f} bp differs from range difference {delta:+.0f} bp."
            )
    return sub, notes


class FomcEventBuilder:
    def meeting_event(self, m: Meeting, ref: SnapshotRef) -> tuple[FinancialEvent, list[SourceLink]]:
        ev = FinancialEvent(
            canonical_event_key=f"FOMC|MEETING|{m.end.isoformat()}",
            event_type=EventCategory.CENTRAL_BANK,
            event_subtype=EventSubtype.MEETING_SCHEDULED,
            event_status=EventStatus.SCHEDULED,
            title=f"FOMC meeting {m.start.isoformat()}–{m.end.isoformat()}"
            + (" (with SEP)" if m.has_projections else ""),
            description="Meeting schedule only; not a policy decision.",
            source_name="Federal Reserve FOMC calendar",
            source_url=CALENDAR_URL,
            source_snapshot_hash=ref.sha256,
            data_origin=ref.origin,
            affected_entities=["FEDFUNDS"],
            scheduled_for=datetime.combine(m.end, time(0), tzinfo=ET),
            availability_precision=AvailabilityPrecision.UNKNOWN,
            verification_status=VerificationStatus.SCHEDULE_ONLY,
            attributes={
                "meeting_start": m.start.isoformat(),
                "meeting_end": m.end.isoformat(),
                "summary_of_economic_projections": m.has_projections,
                "unscheduled": m.unscheduled,
                "scheduled_precision": "DATE_ONLY",
            },
        )
        return ev, [
            SourceLink(
                f"fomc_calendar:{ref.sha256}:{m.end}",
                ev.source_name,
                "schedule",
                CALENDAR_URL,
                ref.snapshot_id,
                ref.sha256,
                ref.retrieved_at,
            )
        ]

    def decision_event(
        self,
        dec: StatementDecision,
        prev: StatementDecision | None,
        ref: SnapshotRef,
        rss_time: datetime | None,
        meeting: Meeting | None,
    ) -> tuple[FinancialEvent, list[SourceLink]]:
        sub, notes = classify(dec, prev)
        if rss_time is not None:
            published, precision, basis = (
                rss_time,
                AvailabilityPrecision.EXACT_TIMESTAMP,
                "Federal Reserve RSS pubDate",
            )
        elif dec.release_time is not None:
            published, precision, basis = (
                dec.release_time,
                AvailabilityPrecision.EXACT_TIMESTAMP,
                "statement 'For release at' header",
            )
        else:
            published, precision, basis = (
                None,
                AvailabilityPrecision.DATE_ONLY_CONSERVATIVE,
                "statement date only",
            )
            notes.append("Publication time not stated; conservative next-day availability applied.")
        avail = published if published is not None else available_at(dec.statement_date)
        comps: list[Comparison] = []
        prev_upper = prev.upper if prev is not None else None
        if dec.upper is not None and prev_upper is not None:
            comps.append(
                Comparison(
                    comparison_id=f"CMP-FOMC-{dec.statement_date}",
                    kind="rate_change",
                    label="Change in upper bound of target range vs previous statement",
                    value=round((dec.upper - prev_upper) * 100, 6),
                    unit=Unit.BASIS_POINTS,
                    formula="(upper[t] - upper[previous statement]) * 100",
                    inputs=[
                        {"statement": dec.url, "upper": dec.upper},
                        {"statement": prev.url, "upper": prev_upper},
                    ],
                )
            )
        rng = (
            f"{dec.lower:.2f}%–{dec.upper:.2f}%"
            if dec.upper is not None and dec.lower is not None
            else "not parsed"
        )
        word = {
            EventSubtype.RATE_HIKE: "raised",
            EventSubtype.RATE_CUT: "lowered",
            EventSubtype.RATE_HOLD: "maintained",
        }.get(sub)
        if word:
            title = f"FOMC {word} the federal funds target range ({dec.statement_date.isoformat()})"
        else:  # not a classifiable rate decision: use the document's own title, no inference
            title = f"Federal Reserve policy statement: {dec.doc_title or 'unclassified'} ({dec.statement_date.isoformat()})"
        ev = FinancialEvent(
            canonical_event_key=f"FOMC|DECISION|{dec.statement_date.isoformat()}",
            event_type=EventCategory.CENTRAL_BANK,
            event_subtype=sub,
            event_status=EventStatus.PUBLISHED,
            title=title,
            description=f"Target range {rng}."
            if dec.upper is not None
            else "Decision not classified from text.",
            source_name="Federal Reserve",
            source_url=dec.url,
            source_document_id=dec.url.rsplit("/", 1)[-1],
            source_snapshot_hash=ref.sha256,
            data_origin=ref.origin,
            affected_entities=["FEDFUNDS", "DGS2", "DGS10"],
            affected_industries=["rates", "banks", "growth_equities"],
            occurred_at=published,
            published_at=published,
            available_at=avail,
            availability_precision=precision,
            series_id="FEDFUNDS_TARGET_UPPER",
            observation_period=dec.statement_date.isoformat(),
            observation_date=dec.statement_date,
            value=dec.upper,
            previous_value=prev_upper,
            unit=Unit.PERCENT,
            revision_status=RevisionStatus.NOT_APPLICABLE,
            verification_status=VerificationStatus.SOURCE_VERIFIED,
            related_event_id=make_event_id(f"FOMC|MEETING|{meeting.end.isoformat()}") if meeting else None,
            event_cluster_id=f"CL-FOMC-{dec.statement_date.isoformat()}",
            comparisons=comps,
            ingested_at=utcnow(),
            attributes={
                "target_range_lower": dec.lower,
                "target_range_upper": dec.upper,
                "action_verb": dec.action,
                "stated_change_bp": dec.change_text_bp,
                "vote": dec.vote,
                "publication_time_basis": basis,
                "classification_notes": notes,
                "previous_statement": prev.url if prev else None,
            },
        )
        link = SourceLink(
            f"fed_statement:{dec.url}",
            "Federal Reserve",
            "primary",
            dec.url,
            ref.snapshot_id,
            ref.sha256,
            ref.retrieved_at,
            excerpt=dec.passage,
        )
        return ev, [link]
