"""Source parsers and builders on SYNTHETIC inline inputs (edge cases real data lacks)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from finance_agent.errors import MalformedDataError
from finance_agent.events.schemas import EventCategory, EventSubtype
from finance_agent.events.sources.calendars import parse_bea_schedule, parse_bls_ics
from finance_agent.events.sources.fomc import StatementDecision, classify, parse_fraction, parse_statement
from finance_agent.events.sources.macro import parse_alfred_csv
from finance_agent.events.sources.sec_events import (
    SecEventBuilder,
    classify_8k,
    parse_acceptance,
    parse_submission_filings,
)
from finance_agent.storage.snapshots import SnapshotRef
from finance_agent.timeutil import ET

REF = SnapshotRef(
    1, "a" * 64, datetime(2026, 11, 1, tzinfo=UTC), "fixture:synthetic", "https://example.test", False
)


# ---------------------------------------------------------------- calendars / DST

ICS = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
DTSTART;TZID=US-Eastern:20260311T083000\r
SUMMARY:Consumer Price Index\r
UID:a\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;TZID=US-Eastern:20260306T083000\r
SUMMARY:Employment Situation\r
UID:b\r
END:VEVENT\r
BEGIN:VEVENT\r
DTSTART;TZID=US-Eastern:20260312T083000\r
SUMMARY:Producer Price Index\r
UID:c\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_bls_ics_dst_transition_and_filtering():
    rel = parse_bls_ics(ICS)
    assert [r.group for r in rel] == ["BLS_EMPSIT", "BLS_CPI"]  # PPI ignored
    # 2026-03-08 is the US DST change: 08:30 ET is 13:30 UTC before, 12:30 UTC after.
    assert rel[0].scheduled_for.astimezone(UTC).hour == 13
    assert rel[1].scheduled_for.astimezone(UTC).hour == 12
    with pytest.raises(MalformedDataError):
        parse_bls_ics("not a calendar")


def test_bea_schedule_requires_known_structure():
    with pytest.raises(MalformedDataError):
        parse_bea_schedule("<html>nothing</html>")


def test_alfred_csv_parsing_and_validation():
    data = parse_alfred_csv(
        b"observation_date,CPIAUCSL_20261014\n2026-08-01,309.0\n2026-09-01,.\n", "CPIAUCSL"
    )
    assert data == {date(2026, 8, 1): 309.0, date(2026, 9, 1): None}
    with pytest.raises(MalformedDataError):
        parse_alfred_csv(b"date,value\n", "CPIAUCSL")
    with pytest.raises(MalformedDataError):
        parse_alfred_csv(b"observation_date,CPIAUCSL_x\n2026-08-01,abc\n", "CPIAUCSL")


# ---------------------------------------------------------------- FOMC


@pytest.mark.parametrize(
    "text,value", [("3-3/4", 3.75), ("4", 4.0), ("1/4", 0.25), ("0", 0.0), ("5-1/2", 5.5)]
)
def test_fed_fractions(text, value):
    assert parse_fraction(text) == value


def statement(sentence: str, date_str="20260916", rel="2:00 p.m. EDT") -> StatementDecision:
    doc = f"<html><p>For release at {rel}</p><p>The Federal Open Market Committee approved ... by a 12-0 vote: {sentence}</p></html>"
    return parse_statement(
        doc, f"https://www.federalreserve.gov/newsevents/pressreleases/monetary{date_str}a.htm"
    )


def test_statement_parsing_hike_hold_cut():
    hike = statement(
        "The Committee decided to raise the target range for the federal funds rate by 1/4 percentage point to 3-3/4 to 4 percent."
    )
    assert (hike.action, hike.lower, hike.upper, hike.change_text_bp) == ("raise", 3.75, 4.0, 25.0)
    assert hike.release_time == datetime(2026, 9, 16, 14, 0, tzinfo=ET) and hike.vote == "12-0"
    hold = statement(
        "The Committee decided to maintain the target range for the federal funds rate at 3-3/4 to 4 percent."
    )
    assert (hold.action, hold.upper, hold.change_text_bp) == ("maintain", 4.0, None)
    winter = statement(
        "The Committee decided to lower the target range for the federal funds rate by 1/2 percentage point to 3-1/4 to 3-1/2 percent.",
        "20261209",
        "2:00 p.m. EST",
    )
    assert winter.release_time.astimezone(UTC).hour == 19 and winter.change_text_bp == 50.0


def test_decision_classification_cross_checks_previous_range():
    prev = statement(
        "The Committee decided to maintain the target range for the federal funds rate at 3-1/2 to 3-3/4 percent.",
        "20260729",
    )
    hike = statement(
        "The Committee decided to raise the target range for the federal funds rate by 1/4 percentage point to 3-3/4 to 4 percent."
    )
    assert classify(hike, prev)[0] == EventSubtype.RATE_HIKE
    assert classify(hike, None)[0] == EventSubtype.RATE_HIKE  # explicit verb suffices without history
    conflicting = statement(
        "The Committee decided to raise the target range for the federal funds rate to 3-1/4 to 3-1/2 percent."
    )
    sub, notes = classify(conflicting, prev)
    assert sub == EventSubtype.POLICY_STATEMENT and "conflicts" in notes[0]


def test_statement_without_decision_sentence_is_not_classified():
    d = statement("The Committee will continue to monitor the implications of incoming information.")
    assert d.action is None and classify(d, None)[0] == EventSubtype.POLICY_STATEMENT


# ---------------------------------------------------------------- SEC


def submissions(rows: list[dict]) -> dict:
    cols = [
        "accessionNumber",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "form",
        "items",
        "primaryDocument",
        "primaryDocDescription",
    ]
    return {
        "cik": "1",
        "name": "SYNTHETIC CO",
        "filings": {"recent": {c: [r.get(c, "") for r in rows] for c in cols}},
    }


ROWS = [
    {
        "accessionNumber": "0000000001-26-000001",
        "filingDate": "2026-10-28",
        "reportDate": "2026-10-28",
        "acceptanceDateTime": "2026-10-28T20:04:44.000Z",
        "form": "8-K",
        "items": "2.02,9.01",
    },
    {
        "accessionNumber": "0000000001-26-000002",
        "filingDate": "2026-10-28",
        "reportDate": "2026-09-30",
        "acceptanceDateTime": "2026-10-28T20:07:39.000Z",
        "form": "10-Q",
        "items": "",
    },
    {
        "accessionNumber": "0000000001-26-000003",
        "filingDate": "2026-11-05",
        "reportDate": "2026-09-30",
        "acceptanceDateTime": "2026-11-05T21:00:00.000Z",
        "form": "10-Q/A",
        "items": "",
    },
    {
        "accessionNumber": "0000000001-26-000004",
        "filingDate": "2026-06-05",
        "reportDate": "2026-06-04",
        "acceptanceDateTime": "2026-06-05T14:36:56.000Z",
        "form": "8-K",
        "items": "5.02",
    },
    {
        "accessionNumber": "0000000001-26-000005",
        "filingDate": "2026-06-06",
        "reportDate": "2026-06-06",
        "acceptanceDateTime": "2026-06-06T14:00:00.000Z",
        "form": "4",
        "items": "",
    },  # insider form: ignored
    {
        "accessionNumber": "0000000001-26-000006",
        "filingDate": "2026-07-01",
        "reportDate": "2026-07-01",
        "acceptanceDateTime": "2026-07-01T14:00:00.000Z",
        "form": "8-K",
        "items": "9.01",
    },
]


def test_acceptance_is_utc():
    assert parse_acceptance("2026-01-28T21:07:34.000Z") == datetime(2026, 1, 28, 21, 7, 34, tzinfo=UTC)
    assert parse_acceptance("") is None


def test_8k_item_classification_priority():
    assert classify_8k(("2.02", "9.01"))[:2] == (
        EventCategory.CORPORATE_EARNINGS,
        EventSubtype.EARNINGS_RELEASE,
    )
    assert classify_8k(("8.01", "5.02"))[1] == EventSubtype.LEADERSHIP_CHANGE
    assert classify_8k(("9.01",))[:2] == (
        EventCategory.CORPORATE_FILING,
        EventSubtype.FORM_8K,
    )  # exhibits only
    assert classify_8k(())[1] == EventSubtype.FORM_8K


def test_filing_events_clusters_amendments_and_cutoff():
    filings = parse_submission_filings(submissions(ROWS))
    assert {f.form for f in filings} == {"8-K", "10-Q", "10-Q/A"}
    evs = {
        e.source_document_id: e for e, _ in SecEventBuilder("SYN", "Synthetic Co").filing_events(filings, REF)
    }
    earn, tenq, amend = (evs[f"0000000001-26-00000{i}"] for i in (1, 2, 3))
    assert earn.event_type == EventCategory.CORPORATE_EARNINGS and earn.available_at == datetime(
        2026, 10, 28, 20, 4, 44, tzinfo=UTC
    )
    assert earn.availability_precision == "EXACT_TIMESTAMP"
    # Earnings 8-K and the 10-Q filed minutes later are one economic cluster (not two shocks).
    assert tenq.event_cluster_id == earn.event_cluster_id and tenq.related_event_id == earn.event_id
    assert amend.event_subtype == EventSubtype.AMENDMENT and amend.supersedes_event_id == tenq.event_id
    assert evs["0000000001-26-000004"].event_subtype == EventSubtype.LEADERSHIP_CHANGE
    assert evs["0000000001-26-000006"].event_subtype == EventSubtype.FORM_8K
    cut = SecEventBuilder("SYN", "Synthetic Co").filing_events(
        filings, REF, as_of=datetime(2026, 10, 28, 20, 5, tzinfo=UTC)
    )
    assert {e.source_document_id for e, _ in cut} == {
        "0000000001-26-000001",
        "0000000001-26-000004",
        "0000000001-26-000006",
    }


def test_submission_parsing_rejects_malformed_payloads():
    with pytest.raises(MalformedDataError):
        parse_submission_filings({"cik": "1"})
    bad = submissions(ROWS)
    bad["filings"]["recent"]["form"] = bad["filings"]["recent"]["form"][:-1]
    with pytest.raises(MalformedDataError):
        parse_submission_filings(bad)
