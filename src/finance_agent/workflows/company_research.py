"""Bounded company-research workflow orchestrated with LangGraph.

The graph is a fixed, linear sequence of nodes (no agent self-loops):

  parse_request → resolve_company → plan_metrics → retrieve_facts → select_periods
  → calculate → analyze_trends → gather_evidence → synthesize → verify → persist

Any node failure routes to ``fail`` which records the error on the run. LangGraph
provides explicit typed state and step-level tracing; persistence of runs,
evidence and reports is done in our own database.
"""

from __future__ import annotations

import itertools
import logging
import time
import uuid
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from finance_agent.agents.company_research import (
    METRIC_GROUPS,
    ParsedRequest,
    Synthesis,
    build_synthesis_prompt,
    parse_request,
    synthesize,
)
from finance_agent.errors import DataUnavailableError, FinanceAgentError
from finance_agent.research.calculations import compute_annual_metrics, compute_quarterly_metrics
from finance_agent.research.formatting import fmt_value
from finance_agent.research.report import render_markdown
from finance_agent.research.trends import Finding, TrendResult, compute_trends, template_findings
from finance_agent.retrieval.evidence import EvidenceItem, build_evidence, numeric_universe
from finance_agent.schemas.financials import CalculationResult, QuarterlyFinancials
from finance_agent.schemas.research import LLMStatus, ResearchReportOut, ResearchRequest
from finance_agent.services import AppContext, CompanyData
from finance_agent.timeutil import utcnow
from finance_agent.verification.checks import (
    CheckResult,
    VerificationReport,
    check_arithmetic,
    check_as_of,
    check_coverage,
    check_numeric_claim,
    check_numeric_claims,
    check_provenance,
    check_references,
    summarize,
)

log = logging.getLogger(__name__)

BASE_METRICS_FOR_GROUP = {
    "revenue_growth": ["revenue"],
    "margins": ["revenue", "gross_profit", "operating_income", "net_income"],
    "free_cash_flow": ["operating_cash_flow", "capex"],
    "capex_intensity": ["capex", "operating_cash_flow", "revenue"],
    "capital_structure": ["cash", "total_debt"],
}
SUPPORT_CALCS = {"margins": ["gross_profit_derived"], "capex_intensity": ["free_cash_flow"]}


RESEARCH_NODES = (
    "parse_request",
    "resolve_company",
    "plan_metrics",
    "retrieve_facts",
    "select_periods",
    "calculate",
    "analyze_trends",
    "gather_evidence",
    "synthesize",
    "verify",
    "persist",
)


class ResearchState(TypedDict, total=False):
    run_id: str
    request: ResearchRequest
    question: str
    parsed: ParsedRequest
    data: CompanyData
    requested_calcs: list[str]
    base_metrics: list[str]
    financials: QuarterlyFinancials
    calcs: list[CalculationResult]
    trends: list[TrendResult]
    findings: list[Finding]
    summary: str
    summary_findings: list[Finding]
    evidence: dict[str, EvidenceItem]
    synthesis: Synthesis
    verification: VerificationReport
    report: ResearchReportOut
    steps: list[dict[str, Any]]
    error: str | None


class CompanyResearchWorkflow:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.graph = self._build()

    # ---------------------------------------------------------------- graph
    def _build(self):
        g = StateGraph(ResearchState)
        order = list(RESEARCH_NODES)
        for name in order:
            g.add_node(name, self._wrap(name, getattr(self, name)))
        g.add_node("fail", self.fail)
        g.add_edge(START, order[0])
        for cur, nxt in itertools.pairwise(order):
            g.add_conditional_edges(
                cur, lambda s, n=nxt: "fail" if s.get("error") else n, {nxt: nxt, "fail": "fail"}
            )
        g.add_edge(order[-1], END)
        g.add_edge("fail", END)
        return g.compile()

    def _wrap(self, name: str, fn):
        def node(state: ResearchState) -> dict[str, Any]:
            t0 = time.perf_counter()
            started = utcnow().isoformat()
            steps = list(state.get("steps", []))
            try:
                update = fn(state) or {}
                steps.append(
                    {"step": name, "status": "ok", "ms": round((time.perf_counter() - t0) * 1000, 1)}
                )
            except FinanceAgentError as exc:
                log.warning("workflow step failed", extra={"step": name, "error": str(exc)})
                update = {"error": f"{name}: {exc}"}
                steps.append({"step": name, "status": "error", "error": str(exc)})
            except Exception as exc:  # unexpected defect: record, never mask as success
                log.exception("workflow step crashed", extra={"step": name})
                update = {"error": f"{name}: unexpected {type(exc).__name__}: {exc}"}
                steps.append({"step": name, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            # Observability only: wall-clock bounds of the node (does not affect the workflow).
            steps[-1].update(started_at=started, finished_at=utcnow().isoformat())
            update["steps"] = steps
            if self.ctx and state.get("run_id"):
                self.ctx.repo.update_run(state["run_id"], steps=steps)
            return update

        return node

    def run(self, request: ResearchRequest) -> ResearchState:
        run_id = str(uuid.uuid4())
        question = request.effective_question()
        self.ctx.repo.create_run(run_id, question, request.model_dump(mode="json"), request.as_of)
        return self.graph.invoke(
            {"run_id": run_id, "request": request, "question": question, "steps": [], "error": None}
        )

    # ---------------------------------------------------------------- nodes
    def parse_request(self, s: ResearchState) -> dict:
        req = s["request"]
        llm = self.ctx.llm if req.use_llm else _NoLLM()
        parsed = parse_request(s["question"], self.ctx.sec.ticker_map(), llm, req.quarters)
        if req.ticker:  # explicit parameters override interpretation
            parsed.ticker = self.ctx.normalize_ticker(req.ticker)
            parsed.method = "explicit" if not req.question else parsed.method
        if req.quarters != 8 or not req.question:
            parsed.quarters = req.quarters
        if not parsed.ticker:
            raise DataUnavailableError("Could not identify a company/ticker in the request.")
        return {"parsed": parsed}

    def resolve_company(self, s: ResearchState) -> dict:
        overview = self.ctx.company(s["parsed"].ticker)  # validates ticker, stores company+filings
        self.ctx.repo.update_run(s["run_id"], ticker=overview.ticker, cik=overview.profile.cik)
        return {}

    def plan_metrics(self, s: ResearchState) -> dict:
        groups = s["parsed"].metric_groups
        calcs = [c for g in groups for c in METRIC_GROUPS[g] + SUPPORT_CALCS.get(g, [])]
        base = sorted({m for g in groups for m in BASE_METRICS_FOR_GROUP[g]})
        return {"requested_calcs": list(dict.fromkeys(calcs)), "base_metrics": base}

    def retrieve_facts(self, s: ResearchState) -> dict:
        return {"data": self.ctx.company_data(s["parsed"].ticker)}

    def select_periods(self, s: ResearchState) -> dict:
        fin = self.ctx.build_financials(s["data"], s["parsed"].quarters, s["request"].as_of)
        if not fin.quarters:
            raise DataUnavailableError("; ".join(fin.warnings) or "No reporting periods available.")
        return {"financials": fin}

    def calculate(self, s: ResearchState) -> dict:
        fin = s["financials"]
        calcs = compute_quarterly_metrics(fin) + compute_annual_metrics(fin)
        wanted = set(s["requested_calcs"])
        return {"calcs": [c for c in calcs if c.name in wanted]}

    def analyze_trends(self, s: ResearchState) -> dict:
        trends = compute_trends(s["calcs"])
        findings = template_findings(trends, s["financials"])
        summary, summary_findings = self._summary(s["financials"], s["calcs"])
        return {
            "trends": trends,
            "findings": findings,
            "summary": summary,
            "summary_findings": summary_findings,
        }

    def gather_evidence(self, s: ResearchState) -> dict:
        return {"evidence": build_evidence(s["financials"], s["calcs"], s["trends"], s["data"].filings)}

    def synthesize(self, s: ResearchState) -> dict:
        req = s["request"]
        if not req.use_llm:
            return {
                "synthesis": Synthesis(
                    executive_summary=None,
                    interpretations=[],
                    limitations=[],
                    provider="none",
                    status="unavailable",
                    dropped=["LLM synthesis disabled for this request."],
                )
            }
        fin = s["financials"]
        prompt = build_synthesis_prompt(
            fin.company_name,
            fin.ticker or "",
            s["trends"],
            s["evidence"],
            [p.label for p in fin.focus()],
            fin.warnings + [m.reason for m in fin.missing][:10],
        )
        return {"synthesis": synthesize(self.ctx.llm, prompt)}

    def verify(self, s: ResearchState) -> dict:
        items, syn, fin = s["evidence"], s["synthesis"], s["financials"]
        verified_findings = s["findings"] + s["summary_findings"]
        checks: list[CheckResult] = [
            check_references(verified_findings, items),
            check_numeric_claims(verified_findings, items),
            check_arithmetic(items),
            check_provenance(items),
            check_as_of(items, s["request"].as_of),
        ]
        # LLM output: drop anything unsupported instead of letting it into the report.
        llm_details: list[str] = []
        kept: list[Finding] = []
        for f in syn.interpretations:
            unknown = [e for e in f.evidence_ids if e not in items]
            bad = check_numeric_claim(f.text, numeric_universe(items, f.evidence_ids))
            if not f.evidence_ids or unknown or bad:
                llm_details.append(
                    f"Dropped {f.finding_id}: "
                    + (f"unknown ids {unknown}; " if unknown else "")
                    + (f"unsupported numbers {bad}" if bad else "")
                    + ("no evidence cited" if not f.evidence_ids else "")
                )
            else:
                kept.append(f)
        if syn.executive_summary and check_numeric_claim(syn.executive_summary, []):
            llm_details.append("Dropped model executive summary: it contained figures.")
            syn.executive_summary = None
        all_ids = list(items)
        lims = []
        for lim in syn.limitations:
            if check_numeric_claim(lim, numeric_universe(items, all_ids)):
                llm_details.append("Dropped a model limitation statement with unsupported figures.")
            else:
                lims.append(lim)
        syn.limitations = lims
        syn.dropped.extend(llm_details)
        syn.interpretations = kept
        checks.append(
            CheckResult(
                name="llm_output_screening",
                status="warning" if llm_details else "passed",
                details=llm_details or [f"LLM synthesis status: {syn.status}"],
            )
        )
        wanted_base = set(s["base_metrics"])
        missing = [f"{m.metric} {m.period_label}: {m.reason}" for m in fin.missing if m.metric in wanted_base]
        missing += [
            f"{c.label} {c.period_label}: " + "; ".join(c.warnings)
            for c in s["calcs"]
            if c.status != "ok" and c.period_label in {p.label for p in fin.focus()}
        ]
        checks.append(check_coverage(missing))
        return {"verification": summarize(checks), "synthesis": syn}

    def persist(self, s: ResearchState) -> dict:
        report = self._assemble(s)
        report.markdown = render_markdown(report)
        evidence = [(e.evidence_id, e.kind, e.payload) for e in s["evidence"].values()]
        self.ctx.repo.save_report(
            s["run_id"], report.model_dump(mode="json"), report.markdown, report.verification.status, evidence
        )
        self.ctx.repo.update_run(s["run_id"], status="completed", completed_at=utcnow())
        return {"report": report}

    def fail(self, s: ResearchState) -> dict:
        self.ctx.repo.update_run(
            s["run_id"],
            status="failed",
            error=s.get("error"),
            completed_at=utcnow(),
            steps=s.get("steps", []),
        )
        return {}

    # ---------------------------------------------------------------- helpers
    def _summary(self, fin: QuarterlyFinancials, calcs: list[CalculationResult]) -> tuple[str, list[Finding]]:
        """Template executive summary for the latest quarter — every figure cited."""
        latest = fin.focus()[-1].label
        by = {(c.name, c.period_label): c for c in calcs if c.status == "ok"}
        parts, ids = [], []
        spec = [
            ("revenue_yoy", "revenue growth YoY"),
            ("operating_margin", "operating margin"),
            ("free_cash_flow", "free cash flow"),
            ("fcf_margin", "FCF margin"),
            ("capex_to_revenue", "CAPEX intensity (CAPEX/revenue)"),
            ("ocf_to_capex", "OCF/CAPEX"),
        ]
        for name, text in spec:
            c = by.get((name, latest))
            if c is not None:
                parts.append(f"{text} {fmt_value(c.value, c.unit)}")
                ids.append(c.calc_id)
        rev = fin.get("revenue", latest)
        if rev is not None:
            parts.insert(0, f"revenue {fmt_value(rev.value, rev.unit)}")
            ids.insert(0, rev.value_id)
        if not parts:
            return f"No verified metrics are available for {latest}.", []
        text = (
            f"In {latest} (most recent reported quarter), {fin.company_name} reported "
            + "; ".join(parts)
            + "."
        )
        return text, [Finding(finding_id="FND-SUMMARY", section="summary", text=text, evidence_ids=ids)]

    def _assemble(self, s: ResearchState) -> ResearchReportOut:
        fin, syn, items = s["financials"], s["synthesis"], s["evidence"]
        focus_labels = {p.label for p in fin.focus()}
        wanted_base = set(s["base_metrics"]) | {"revenue"}
        metric_values = [
            v
            for m, row in fin.series.items()
            if m in wanted_base
            for v in row
            if v is not None and v.period.label in focus_labels
        ]
        limitations = list(fin.warnings)
        limitations += [
            f"{m.metric} unavailable for {m.period_label}: {m.reason}"
            for m in fin.missing
            if m.metric in wanted_base
        ]
        derived = sum(1 for v in metric_values if v.method == "derived_ytd_difference")
        if derived:
            limitations.append(
                f"{derived} quarterly values are derived from cumulative YTD/annual "
                f"filings (marked †); they are not directly reported quarterly figures."
            )
        concepts = {(v.metric, v.concept) for v in metric_values}
        for metric in {m for m, _ in concepts}:
            used = sorted(c for m, c in concepts if m == metric)
            if len(used) > 1:
                limitations.append(
                    f"{metric} uses different XBRL concepts across periods: {', '.join(used)}."
                )
        for v in metric_values:
            limitations.extend(w for w in v.warnings if "CAPEX uses" in w or "restricted" in w)
        limitations.append(
            "Filing availability uses a conservative next-day policy (filing date + 1 "
            "day, 00:00 New York); intraday acceptance times are not used."
        )
        limitations.append(
            "Only SEC XBRL financial statement data is used; no MD&A text, guidance, "
            "segment data, or market prices are analysed in v0.1."
        )
        if syn.status != "generated":
            limitations.append("LLM synthesis unavailable; narrative is a template-based factual summary.")
        limitations.extend(f"(model) {x}" for x in syn.limitations)
        limitations = list(dict.fromkeys(limitations))
        return ResearchReportOut(
            run_id=s["run_id"],
            status="completed",
            question=s["question"],
            company_name=fin.company_name,
            ticker=fin.ticker or "",
            cik=fin.cik,
            analysis_timestamp=utcnow(),
            as_of=s["request"].as_of,
            availability_policy=fin.availability_policy,
            data_origin=fin.data_origin,
            reporting_periods=fin.focus(),
            requested_metrics=s["requested_calcs"],
            metric_values=metric_values,
            derived_calculations=[
                c for c in s["calcs"] if c.period_label in focus_labels or c.name == "annual_revenue_yoy"
            ],
            trends=s["trends"],
            executive_summary=s["summary"],
            main_findings=s["summary_findings"] + s["findings"],
            model_interpretations=syn.interpretations,
            model_executive_summary=syn.executive_summary,
            source_references=[e for e in items.values() if e.kind == "filing"],
            snapshots=fin.snapshots,
            missing_data=fin.missing,
            limitations=limitations,
            verification=s["verification"],
            llm=LLMStatus(provider=syn.provider, status=syn.status, notes=syn.dropped),
        )


class _NoLLM:
    name = "none"
    available = False

    def complete_json(self, **_):  # pragma: no cover
        raise RuntimeError("disabled")


def run_company_research(ctx: AppContext, request: ResearchRequest) -> ResearchState:
    return CompanyResearchWorkflow(ctx).run(request)
