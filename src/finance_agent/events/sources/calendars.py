"""Official release calendars: BLS (iCalendar) and BEA (HTML schedule).

A calendar entry is a *schedule*, never evidence that a release occurred.
Sources (robots.txt permits these paths):
  https://www.bls.gov/schedule/news_release/bls.ics   (all BLS releases, TZID US-Eastern)
  https://www.bea.gov/news/schedule                    (upcoming BEA releases)
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time

from finance_agent.errors import MalformedDataError
from finance_agent.timeutil import ET

BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BEA_SCHEDULE_URL = "https://www.bea.gov/news/schedule"

# Release groups: one scheduled publication that carries several indicators.
RELEASE_GROUPS = {
    "BLS_CPI": {"name": "Consumer Price Index", "agency": "BLS", "match": r"^Consumer Price Index$"},
    "BLS_EMPSIT": {"name": "Employment Situation", "agency": "BLS", "match": r"^Employment Situation$"},
    "BEA_PIO": {
        "name": "Personal Income and Outlays",
        "agency": "BEA",
        "match": r"^Personal Income and Outlays",
    },
    "BEA_GDP": {
        "name": "Gross Domestic Product",
        "agency": "BEA",
        "match": r"^GDP \((Advance|Second|Third)|^Gross Domestic Product",
    },
}


@dataclass(frozen=True)
class ScheduledRelease:
    group: str
    release_name: str  # as published by the agency
    scheduled_for: datetime  # timezone-aware
    source_uid: str  # ICS UID or BEA row identity
    time_known: bool = True


def _group_for(title: str, agency: str) -> str | None:
    for gid, g in RELEASE_GROUPS.items():
        if g["agency"] == agency and re.search(g["match"], title.strip(), re.IGNORECASE):
            return gid
    return None


def parse_bls_ics(text: str) -> list[ScheduledRelease]:
    """Parse the BLS iCalendar feed (times are US-Eastern local)."""
    if "BEGIN:VCALENDAR" not in text:
        raise MalformedDataError("BLS schedule is not an iCalendar document")
    unfolded = re.sub(r"\r?\n[ \t]", "", text).replace("\r", "")
    out: list[ScheduledRelease] = []
    for block in unfolded.split("BEGIN:VEVENT")[1:]:
        fields: dict[str, str] = {}
        for line in block.split("\n"):
            if ":" in line:
                k, v = line.split(":", 1)
                fields[k.strip()] = v.strip()
        summary = fields.get("SUMMARY", "").replace("\\,", ",")
        group = _group_for(summary, "BLS")
        if group is None:
            continue
        start_key = next((k for k in fields if k.startswith("DTSTART")), None)
        if start_key is None:
            continue
        raw = fields[start_key]
        try:
            if "T" in raw:
                local = datetime.strptime(raw.rstrip("Z"), "%Y%m%dT%H%M%S")
                # 'Z' suffix = UTC; otherwise the feed's TZID (US-Eastern) applies.
                dt = local.replace(tzinfo=UTC) if raw.endswith("Z") else local.replace(tzinfo=ET)
                time_known = True
            else:
                dt = datetime.combine(datetime.strptime(raw, "%Y%m%d").date(), time(0), tzinfo=ET)
                time_known = False
        except ValueError:
            continue
        out.append(ScheduledRelease(group, summary, dt, fields.get("UID", raw), time_known))
    return sorted(out, key=lambda r: (r.scheduled_for, r.group))


_BEA_ROW = re.compile(r"<tr[^>]*scheduled-releases-type[^>]*>(.*?)</tr>", re.S | re.I)


def parse_bea_schedule(text: str) -> list[ScheduledRelease]:
    """Parse BEA's schedule table: year heading, 'Month Day', 'H:MM AM', release title."""
    year_m = re.search(r"Year\s+(\d{4})", text)
    if not year_m or "scheduled-releases" not in text:
        raise MalformedDataError("BEA schedule page structure not recognized")
    out: list[ScheduledRelease] = []
    # Walk the document so year headings apply to following rows.
    pos_years = [(m.start(), int(m.group(1))) for m in re.finditer(r"Year\s+(\d{4})", text)]
    for m in _BEA_ROW.finditer(text):
        year = max((y for p, y in pos_years if p < m.start()), default=int(year_m.group(1)))
        cells = [
            html.unescape(re.sub(r"<[^>]+>", " ", c))
            for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)
        ]
        cells = [re.sub(r"\s+", " ", c).strip() for c in cells]
        joined = " | ".join(cells)
        d = re.search(
            r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})",
            joined,
        )
        t = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)", joined, re.I)
        title = max(cells, key=len) if cells else ""
        group = _group_for(title, "BEA")
        if not d or group is None:
            continue
        month = datetime.strptime(d.group(1), "%B").month
        day_ = date(year, month, int(d.group(2)))
        if t:
            hh = int(t.group(1)) % 12 + (12 if t.group(3).upper() == "PM" else 0)
            dt = datetime.combine(day_, time(hh, int(t.group(2))), tzinfo=ET)
        else:
            dt = datetime.combine(day_, time(0), tzinfo=ET)
        out.append(ScheduledRelease(group, title, dt, f"BEA|{day_}|{title}", bool(t)))
    return sorted(out, key=lambda r: (r.scheduled_for, r.group))
