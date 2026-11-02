"""Offline end-to-end research workflow, report contract and LLM-output screening."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from finance_agent.agents.company_research import parse_request_deterministic
from finance_agent.schemas.research import ResearchReportOut, ResearchRequest
from finance_agent.verification.checks import check_numeric_claim
from finance_agent.workflows.company_research import CompanyResearchWorkflow

QUESTION = (
    "Analyze Microsoft's revenue growth, operating margins, free cash flow, and capital "
    "expenditure intensity over the most recent eight reported quarters."
)


class FakeLLM:
    """Deterministic stand-in for Claude. Returns one supported and one fabricated claim."""

    name = "fake-llm"
    available = True

    def __init__(self):
        self.prompts = []

    def complete_json(self, *, system, user, schema, max_tokens=16000):
        self.prompts.append(user)
        trend_id = next(line.split(" | ")[0] for line in user.splitlines() if line.startswith("T-"))
        displayed = user.split(trend_id)[1].split("->")[1].split("(")[0].strip()
        return {
            "executive_summary": "Growth accelerated while capital intensity rose sharply.",
            "interpretations": [
                {
                    "text": f"The latest reading of {displayed} suggests momentum (hypothesis).",
                    "evidence_ids": [trend_id],
                },
                {"text": "Revenue will grow 37.5% next year.", "evidence_ids": [trend_id]},
                {"text": "Cloud demand is strong.", "evidence_ids": ["C-nonexistent"]},
            ],
            "limitations": ["Segment data was not analysed."],
        }


def test_parse_question():
    import json

    from finance_agent.providers.sec import parse_company_tickers
    from tests.conftest import FIXTURES

    tm = parse_company_tickers(json.loads((FIXTURES / "sec/company_tickers.json").read_text()))
    p = parse_request_deterministic(QUESTION, tm)
    assert p.ticker == "MSFT" and p.quarters == 8
    assert set(p.metric_groups) == {"revenue_growth", "margins", "free_cash_flow", "capex_intensity"}
    assert parse_request_deterministic("How is $AAPL doing over 4 quarters?", tm).quarters == 4


def test_offline_research_report_contract(ctx):
    state = CompanyResearchWorkflow(ctx).run(ResearchRequest(question=QUESTION, use_llm=False))
    assert not state.get("error"), state.get("error")
    rep: ResearchReportOut = state["report"]
    assert (rep.ticker, rep.cik) == ("MSFT", "0000789019")
    assert len(rep.reporting_periods) == 8 and rep.data_origin == "fixture:real_snapshot"
    assert rep.verification.status in {"passed", "passed_with_warnings"}
    assert {c.name for c in rep.verification.checks} >= {
        "evidence_references",
        "numeric_claims",
        "arithmetic_consistency",
        "provenance_complete",
        "as_of_compliance",
        "data_availability",
        "llm_output_screening",
    }
    assert rep.llm.status == "unavailable" and "template-based" in rep.markdown
    for section in [
        "## 1. Executive summary",
        "## 2. Financial performance",
        "## 3. Growth and margin analysis",
        "## 4. Cash flow and capital expenditures",
        "## 5. Key trends",
        "## 6. Evidence and references",
        "## 7. Data limitations",
    ]:
        assert section in rep.markdown
    assert "OFFLINE FIXTURE DATA" in rep.markdown
    # Every finding's evidence resolves; every metric value has full provenance.
    ids = {e.evidence_id for e in ctx.repo.evidence_for_run(rep.run_id)}
    for f in rep.main_findings:
        assert set(f.evidence_ids) <= ids
    for v in rep.metric_values:
        assert v.sources and all(s.accession and s.filing_url and s.concept for s in v.sources)
    for c in rep.derived_calculations:
        assert c.status != "ok" or all(i.ref_id in ids for i in c.inputs)
    assert all(e.payload.get("index_url") for e in rep.source_references)


@pytest.mark.parametrize("ticker", ["AAPL", "AMZN", "GOOGL", "META"])
def test_offline_research_other_initial_companies(ctx, ticker):
    state = CompanyResearchWorkflow(ctx).run(ResearchRequest(ticker=ticker, quarters=8, use_llm=False))
    assert not state.get("error"), state.get("error")
    rep = state["report"]
    assert rep.verification.status != "failed"
    assert len(rep.reporting_periods) == 8


def test_amazon_gross_margin_is_flagged_and_capex_fallback_disclosed(ctx):
    state = CompanyResearchWorkflow(ctx).run(ResearchRequest(ticker="AMZN", quarters=4, use_llm=False))
    rep = state["report"]
    gm = [c for c in rep.derived_calculations if c.name == "gross_margin"]
    assert gm and all(any("GrossProfit not reported" in w for w in c.warnings) for c in gm)
    assert any("PaymentsToAcquireProductiveAssets" in x for x in rep.limitations)


def test_llm_output_is_screened(ctx):
    fake = FakeLLM()
    ctx.llm = fake
    state = CompanyResearchWorkflow(ctx).run(ResearchRequest(question=QUESTION, use_llm=True))
    rep = state["report"]
    assert rep.llm.status == "generated" and fake.prompts
    texts = [f.text for f in rep.model_interpretations]
    assert len(texts) == 1 and "momentum" in texts[0]  # supported claim kept
    assert not any("37.5%" in t for t in texts)  # fabricated forecast dropped
    assert all(f.kind == "model_interpretation" for f in rep.model_interpretations)
    assert rep.model_executive_summary and check_numeric_claim(rep.model_executive_summary, []) == []
    screening = next(c for c in rep.verification.checks if c.name == "llm_output_screening")
    assert screening.status == "warning" and len(screening.details) == 2
    assert "Model-generated interpretation" in rep.markdown and "(model) Segment data" in rep.markdown


def test_llm_summary_with_numbers_is_dropped(ctx):
    class NumberyLLM(FakeLLM):
        def complete_json(self, **kw):
            out = super().complete_json(**kw)
            out["executive_summary"] = "Revenue grew 18.8% to a record."
            return out

    ctx.llm = NumberyLLM()
    rep = CompanyResearchWorkflow(ctx).run(ResearchRequest(ticker="MSFT", use_llm=True))["report"]
    assert rep.model_executive_summary is None


def test_cli_offline_commands(tmp_path, monkeypatch):
    from finance_agent.config.settings import get_settings
    from finance_agent.interfaces.cli import app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'cli.db').as_posix()}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        r = runner.invoke(app, ["financials", "MSFT", "--quarters", "4", "--offline"])
        assert r.exit_code == 0, r.output
        assert "MICROSOFT" in r.output and "operating_margin" in r.output
        r = runner.invoke(app, ["research", "MSFT", "--quarters", "4", "--offline", "--no-llm"])
        assert r.exit_code == 0, r.output
        assert "Company Research Report" in r.output
        r = runner.invoke(app, ["macro", "GDP", "--offline"])
        assert r.exit_code == 0 and "SYNTHETIC" in r.output
        r = runner.invoke(app, ["financials", "NOTATICKER1", "--offline"])
        assert r.exit_code == 1
        assert runner.invoke(app, ["--help"]).exit_code == 0
    finally:
        get_settings.cache_clear()
