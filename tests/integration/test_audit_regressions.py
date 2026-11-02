"""Regression tests for defects confirmed in the v0.1 baseline audit (2026-11-02).

Each test reproduces the failure observed before the fix.
"""

from __future__ import annotations

import threading
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

from finance_agent.agents.llm import NullLLM
from finance_agent.interfaces.api import create_app
from finance_agent.research.concepts import METRICS
from finance_agent.research.normalize import FiscalCalendar, Normalizer
from finance_agent.schemas.research import ResearchRequest
from finance_agent.services import AppContext
from finance_agent.storage.db import Database
from finance_agent.storage.models import FinancialFact, SourceDocument, SourceSnapshot
from finance_agent.timeutil import parse_as_of
from finance_agent.verification.checks import check_numeric_claim
from tests.conftest import offline_settings
from tests.helpers import fact

# ---------------------------------------------------------------- 1. as_of semantics


@pytest.mark.parametrize("value", ["2025-07-30", date(2025, 7, 30)])
def test_post_body_date_only_as_of_matches_cli_and_get(value):
    """Before: POST parsed '2025-07-30' as naive midnight UTC (8 pm ET the previous day)."""
    req = ResearchRequest(ticker="MSFT", as_of=value)
    assert req.as_of == parse_as_of("2025-07-30") == datetime(2025, 7, 31, 3, 59, 59, tzinfo=UTC)


def test_post_body_naive_datetime_is_utc_and_bad_value_rejected():
    assert ResearchRequest(ticker="MSFT", as_of="2025-07-30T12:00:00").as_of == datetime(
        2025, 7, 30, 12, tzinfo=UTC
    )
    with pytest.raises(ValidationError):
        ResearchRequest(ticker="MSFT", as_of="30/07/2025")


def test_api_post_date_only_as_of_end_to_end(tmp_path):
    s = offline_settings(tmp_path)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db, llm=NullLLM())
    with TestClient(create_app(s, ctx)) as c:
        r = c.post("/research/company", json={"ticker": "MSFT", "quarters": 4, "as_of": "2025-07-30"})
        rep = r.json()["report"]
        assert rep["as_of"] == "2025-07-31T03:59:59Z"
        assert rep["reporting_periods"][-1]["label"] == "FY2025 Q3"  # 10-K filed 07-30 not yet usable
        assert c.post("/research/company", json={"ticker": "MSFT", "as_of": "garbage"}).status_code == 422


# ---------------------------------------------------------------- 2. debt measurement basis


def _debt(facts, end="2024-03-31"):
    norm = Normalizer(facts, FiscalCalendar(12), None)
    keys = (
        "long_term_debt_total",
        "long_term_debt_noncurrent",
        "long_term_debt_current",
        "short_term_borrowings",
    )
    return norm.total_debt(date.fromisoformat(end), {k: norm.instant(METRICS[k]) for k in keys})


def test_carrying_components_preferred_over_face_value_total():
    """Before: LongTermDebt (often face value) won whenever present, mixing bases over time."""
    v = _debt(
        [
            fact("LongTermDebt", None, "2024-03-31", 25_030),  # face/principal
            fact("LongTermDebtNoncurrent", None, "2024-03-31", 23_930),  # carrying
            fact("LongTermDebtCurrent", None, "2024-03-31", 1_000),
        ]
    )
    assert v.value == 24_930 and v.formula == "LongTermDebtNoncurrent + LongTermDebtCurrent"


def test_long_term_debt_total_used_only_as_flagged_fallback():
    v = _debt(
        [
            fact("LongTermDebt", None, "2024-03-31", 100),
            fact("LongTermDebtNoncurrent", None, "2024-03-31", 90),
        ]
    )
    assert v.value == 100 and v.formula == "LongTermDebt"
    assert any("face/principal" in w for w in v.warnings)


def test_amazon_total_debt_uses_one_basis_for_all_quarters(ctx):
    fin = ctx.financials("AMZN", quarters=8).financials
    formulas = {v.formula for v in fin.series["total_debt"][-8:] if v}
    assert all(f.startswith("LongTermDebtNoncurrent + LongTermDebtCurrent") for f in formulas), formulas


# ---------------------------------------------------------------- 3. snapshot cache freshness


def test_unchanged_refetch_refreshes_cache_without_mutating_content(ctx):
    """Before: dedup kept the first retrieved_at, so after one TTL every call re-fetched."""
    ctx.sec.submissions("0000789019")
    with ctx.db.session() as s:
        snap = s.scalars(select(SourceSnapshot)).one()
        first = (snap.sha256, snap.retrieved_at, snap.storage_path)
        snap.retrieved_at -= timedelta(hours=13)
        snap.last_retrieved_at -= timedelta(hours=13)
        aged_first_seen = snap.retrieved_at
    counts = []
    for _ in range(3):
        n = ctx.http.request_count
        ctx.sec.submissions("0000789019")
        counts.append(ctx.http.request_count - n)
    assert counts == [1, 0, 0]
    with ctx.db.session() as s:
        snap = s.scalars(select(SourceSnapshot)).one()  # still one immutable content row
        assert (snap.sha256, snap.storage_path) == (first[0], first[2])
        assert snap.retrieved_at == aged_first_seen  # first-seen time is never rewritten
        assert snap.last_retrieved_at > snap.retrieved_at


# ---------------------------------------------------------------- 4. concurrent writes


def test_concurrent_first_loads_do_not_fail(tmp_path):
    """Before: 3 of 4 threads failed (UNIQUE constraint race; shared temp file on Windows)."""
    s = offline_settings(tmp_path)
    db = Database(s.database_url)
    db.create_all()
    errors: list[str] = []
    barrier = threading.Barrier(4)

    def work():
        c = AppContext(s, db=db)
        barrier.wait()
        try:
            c.company_data("MSFT")
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(f"{type(exc).__name__}: {exc}")
        finally:
            c.close()

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with db.session() as sess:
        n_facts = sess.scalar(select(func.count()).select_from(FinancialFact))
        n_docs = sess.scalar(select(func.count()).select_from(SourceDocument))
        dup_snaps = sess.execute(
            select(SourceSnapshot.source_document_id, func.count())
            .group_by(SourceSnapshot.source_document_id, SourceSnapshot.sha256)
            .having(func.count() > 1)
        ).all()
    assert n_facts == len({f.fact_hash for f in AppContext(s, db=db).repo.load_facts("0000789019")})
    assert n_docs == 3 and dup_snaps == []  # tickers, submissions, companyfacts


# ---------------------------------------------------------------- 5. numeric claim bypass


@pytest.mark.parametrize(
    "text,supported",
    [
        ("Operating margin reached 51.", False),  # before: silently accepted
        ("Revenue grew by 37 over the period.", False),
        ("Operating margin reached 49.8%.", True),
        ("Trend measured over 8 quarters.", True),
        ("Based on 10-K and 10-Q/A filings for 2025.", True),
        ("Apple uses a 52/53-week fiscal calendar; one 53-week year.", True),
    ],
)
def test_unitless_integers_must_be_supported(text, supported):
    assert (check_numeric_claim(text, [(0.498, "ratio")]) == []) is supported


def test_llm_summary_with_unitless_number_is_dropped(ctx):
    from finance_agent.workflows.company_research import CompanyResearchWorkflow
    from tests.integration.test_workflow_offline import FakeLLM

    class Sneaky(FakeLLM):
        def complete_json(self, **kw):
            out = super().complete_json(**kw)
            out["executive_summary"] = "Margins expanded by roughly 4 points as growth hit 19."
            return out

    ctx.llm = Sneaky()
    rep = CompanyResearchWorkflow(ctx).run(ResearchRequest(ticker="MSFT", use_llm=True))["report"]
    assert rep.model_executive_summary is None
