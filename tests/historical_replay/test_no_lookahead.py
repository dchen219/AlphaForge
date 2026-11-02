"""Historical replay: the system must never use information published after ``as_of``.

Availability policy under test (timeutil.py): a source dated D (SEC ``filed`` or
ALFRED ``realtime_start``) is usable only from D+1 00:00 America/New_York.

SEC cases use the REAL Microsoft companyfacts snapshot in tests/fixtures/sec
(FY2025 10-K accession 0000950170-25-100235 filed 2025-07-30; FY2025 Q3 10-Q filed
2025-04-30). FRED cases use SYNTHETIC vintage fixtures.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from finance_agent.providers.fred import parse_observations, select_vintage
from finance_agent.research.calculations import compute_quarterly_metrics
from finance_agent.research.normalize import FiscalCalendar, build_quarterly_financials
from finance_agent.research.trends import compute_trends
from finance_agent.retrieval.evidence import build_evidence
from finance_agent.schemas.research import ResearchRequest
from finance_agent.schemas.sec import XbrlFact
from finance_agent.timeutil import ET
from finance_agent.verification.checks import check_as_of
from finance_agent.workflows.company_research import CompanyResearchWorkflow
from tests.conftest import FIXTURES

MSFT = "0000789019"
TENK_FY2025 = "0000950170-25-100235"


def et(y, m, d, hh=12, mm=0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET).astimezone(UTC)


@pytest.fixture(scope="module")
def msft_facts() -> list[XbrlFact]:
    from finance_agent.providers.sec import parse_companyfacts

    payload = json.loads((FIXTURES / "sec/companyfacts/CIK0000789019.json").read_text(encoding="utf-8"))
    return parse_companyfacts(payload).facts


def table(facts, as_of, quarters=8):
    return build_quarterly_financials(
        facts,
        cik=MSFT,
        company_name="MSFT",
        ticker="MSFT",
        calendar=FiscalCalendar(6),
        as_of=as_of,
        quarters=quarters,
    )


def all_sources(fin):
    return [s for row in fin.series.values() for v in row if v for s in v.sources] + [
        s for vals in fin.annual.values() for v in vals for s in v.sources
    ]


# ------------------------------------------------------------------ SEC: included vs excluded


def test_10k_filed_on_cutoff_day_is_excluded(msft_facts):
    """10-K filed 2025-07-30; at 23:00 ET that day it is still NOT usable."""
    fin = table(msft_facts, et(2025, 7, 30, 23))
    assert fin.quarters[-1].label == "FY2025 Q3"
    assert all(s.accession != TENK_FY2025 for s in all_sources(fin))
    assert all(s.filed < date(2025, 7, 30) for s in all_sources(fin))
    assert fin.annual["revenue"][-1].period.label == "FY2024"


def test_10k_is_included_from_next_day(msft_facts):
    fin = table(msft_facts, et(2025, 7, 31, 0, 1))
    q4 = fin.series["revenue"][-1]
    assert fin.quarters[-1].label == "FY2025 Q4" and q4.method == "derived_ytd_difference"
    assert TENK_FY2025 in {s.accession for s in q4.sources}
    assert fin.annual["revenue"][-1].period.label == "FY2025"


def test_later_comparatives_are_not_used_for_past_periods(msft_facts):
    """Later filings re-report FY2025 Q3 as comparatives; at as_of they did not exist yet."""
    as_of = et(2025, 5, 15)
    fin = table(msft_facts, as_of)
    assert fin.quarters[-1].label == "FY2025 Q3"
    for s in all_sources(fin):
        assert s.available_at <= as_of, (s.accession, s.filed)
    latest = table(msft_facts, None)
    later = [s for s in all_sources(latest) if s.available_at > as_of]
    assert later, "sanity: the unrestricted view does contain post-cutoff sources"


def test_future_restatement_is_invisible_before_filing(msft_facts):
    """NEGATIVE TEST: inject a (synthetic) 10-Q/A restating FY2025 Q3 revenue after the cutoff."""
    base = table(msft_facts, et(2025, 8, 15))
    q3 = base.get("revenue", "FY2025 Q3")
    assert q3 is not None
    restated = XbrlFact(
        cik=MSFT,
        taxonomy="us-gaap",
        concept=q3.concept,
        unit="USD",
        start=q3.period.start,
        end=q3.period.end,
        value=q3.value + 1e9,
        accession="9999999999-25-000001",
        form="10-Q/A",
        filed=date(2025, 9, 15),
    )
    facts = [*msft_facts, restated]
    before = table(facts, et(2025, 8, 15)).get("revenue", "FY2025 Q3")
    assert before.value == q3.value and not before.alternative_values
    assert "9999999999-25-000001" not in {s.accession for s in before.sources}
    on_day = table(facts, et(2025, 9, 15, 20)).get("revenue", "FY2025 Q3")
    assert on_day.value == q3.value  # same calendar day: still excluded
    after = table(facts, et(2025, 9, 16, 9)).get("revenue", "FY2025 Q3")
    assert after.value == q3.value + 1e9 and after.restated


def test_leakage_guard_detects_post_cutoff_evidence(msft_facts):
    """NEGATIVE TEST: evidence built without the cutoff must FAIL the as-of check."""
    as_of = et(2025, 5, 15)
    leaky = table(msft_facts, None)  # simulates a pipeline that ignored as_of
    calcs = compute_quarterly_metrics(leaky)
    items = build_evidence(leaky, calcs, compute_trends(calcs), [])
    res = check_as_of(items, as_of)
    assert res.status == "failed" and res.details
    clean = table(msft_facts, as_of)
    c2 = compute_quarterly_metrics(clean)
    assert check_as_of(build_evidence(clean, c2, compute_trends(c2), []), as_of).status == "passed"


def test_workflow_replay_respects_cutoff(ctx):
    as_of = et(2025, 5, 15)
    state = CompanyResearchWorkflow(ctx).run(
        ResearchRequest(ticker="MSFT", quarters=4, as_of=as_of, use_llm=False)
    )
    assert not state.get("error"), state.get("error")
    rep = state["report"]
    assert rep.reporting_periods[-1].label == "FY2025 Q3"
    assert next(c for c in rep.verification.checks if c.name == "as_of_compliance").status == "passed"
    for e in rep.source_references:
        assert e.payload["filing_date"] < "2025-05-15"
    for v in rep.metric_values:
        assert all(s.available_at <= as_of for s in v.sources)


# ------------------------------------------------------------------ FRED vintages (synthetic)


@pytest.fixture(scope="module")
def cpi_rows():
    payload = json.loads((FIXTURES / "fred/observations/CPIAUCSL.json").read_text(encoding="utf-8"))
    return parse_observations(payload)


def _et_on(d: date, hh: int) -> datetime:
    return et(d.year, d.month, d.day, hh)


def test_macro_replay_included_and_excluded(cpi_rows):
    obs = date(2024, 3, 1)
    versions = sorted((r for r in cpi_rows if r.observation_date == obs), key=lambda r: r.realtime_start)
    first, revised = versions[0], versions[1]

    def value_at(as_of):
        return {r.observation_date: r.value for r in select_vintage(cpi_rows, as_of)}.get(obs, "absent")

    assert value_at(_et_on(first.realtime_start, 23)) == "absent"  # release day: excluded
    assert value_at(_et_on(first.realtime_start + timedelta(days=1), 9)) == first.value
    assert value_at(_et_on(revised.realtime_start, 23)) == first.value  # revision not yet usable
    assert value_at(_et_on(revised.realtime_start + timedelta(days=1), 9)) == revised.value
    assert value_at(None) == versions[-1].value


def test_macro_replay_matches_independent_vintage_oracle(ctx, cpi_rows):
    leaked_revision_checked = False
    latest = {r.observation_date: r.value for r in select_vintage(cpi_rows, None)}
    for as_of in [et(2023, 6, 1), et(2024, 1, 15), et(2024, 11, 30)]:
        res = ctx.macro("CPIAUCSL", as_of=as_of)
        vintage = res.effective_vintage_date
        assert res.observations, as_of
        # Oracle: from the raw fixture rows, the version whose window contains the vintage date.
        oracle = {
            r.observation_date: r.value for r in cpi_rows if r.realtime_start <= vintage <= r.realtime_end
        }
        got = {o.observation_date: o.value for o in res.observations}
        assert got == oracle
        for o in res.observations:
            assert o.available_at <= as_of
        # Observations first published after the vintage are absent.
        assert all(min(r.realtime_start for r in cpi_rows if r.observation_date == d) <= vintage for d in got)
        if any(got[d] != latest[d] for d in got):
            leaked_revision_checked = True  # an older vintage value differs from today's revised value
    assert leaked_revision_checked, "fixture must contain revisions after some replay date"
