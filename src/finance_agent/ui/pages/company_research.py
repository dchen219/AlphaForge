"""Page A — Company Research (v0.1): fundamentals, deterministic metrics, evidence-linked report."""

from __future__ import annotations

import streamlit as st

from finance_agent.research.formatting import fmt_value
from finance_agent.schemas.research import ResearchRunOut
from finance_agent.services import CompanyOverview, FinancialsResponse
from finance_agent.ui import components as ui
from finance_agent.ui import validation

CHART_RATIOS = ("gross_margin", "operating_margin", "net_margin", "fcf_margin", "capex_to_revenue")


def render() -> None:
    st.title("Company Research")
    st.caption(
        "SEC XBRL facts → point-in-time normalization → deterministic calculations → verified report "
        "(LangGraph `company_research`). The LLM never supplies a number."
    )
    mode = ui.mode_selector()
    c, h = ui.backend(mode)
    if c is None:
        return
    ui.backend_caption(h, c)

    col = st.columns([1, 1, 1])
    raw_ticker = col[0].text_input("Ticker", value="MSFT", max_chars=10, key="A_ticker")
    n_quarters = col[1].number_input("Quarters", min_value=2, max_value=20, value=8, step=1, key="A_q")
    llm_ok = h.credentials_configured.get("anthropic_api_key", False) and h.llm_provider != "none"
    use_llm = col[2].toggle(
        "LLM interpretation",
        value=False,
        key="A_llm",
        help="Optional narrative. Without credentials/credit the report falls back to the deterministic "
        "template and records llm.status.",
    )
    if use_llm and not llm_ok:
        st.info(
            f"LLM provider is `{h.llm_provider}` (Anthropic key configured: "
            f"{h.credentials_configured.get('anthropic_api_key')}). The workflow will run and record the "
            "deterministic fallback."
        )
    as_of = ui.cutoff_input(
        "Analysis as-of cutoff",
        "A_asof",
        help="Only sources available by this instant are used (next-day policy for date-stamped filings).",
    )

    b1, b2 = st.columns(2)
    if b1.button("Retrieve fundamentals", type="secondary", width="stretch"):
        try:
            t = validation.ticker(raw_ticker)
            q = validation.quarters(n_quarters)
            with st.spinner(f"GET /companies/{t} and /financials …"):
                st.session_state["A_company"] = c.company(t)
                st.session_state["A_fin"] = c.financials(t, q, as_of)
                st.session_state["A_fin_mode"] = mode
        except Exception as exc:
            ui.show_error(exc, "Retrieving fundamentals")
    if b2.button("Run company research", type="primary", width="stretch"):
        try:
            t = validation.ticker(raw_ticker)
            q = validation.quarters(n_quarters)
            with st.spinner("POST /research/company — running the LangGraph workflow …"):
                st.session_state["A_run"] = c.research(t, q, as_of, use_llm)
                st.session_state["A_run_mode"] = mode
        except Exception as exc:
            ui.show_error(exc, "Company research")

    tab_f, tab_r = st.tabs(["Fundamentals & metrics", "Research report"])
    with tab_f:
        if "A_fin" in st.session_state:
            _fundamentals(st.session_state.get("A_company"), st.session_state["A_fin"])
        else:
            st.info("Click **Retrieve fundamentals** to load normalized quarterly data and calculations.")
    with tab_r:
        if "A_run" in st.session_state:
            _report(st.session_state["A_run"])
        else:
            st.info("Click **Run company research** to execute the workflow.")


def _fundamentals(co: CompanyOverview | None, data: FinancialsResponse) -> None:
    fin = data.financials
    ui.origin_banner(fin.data_origin)
    if co is not None:
        p = co.profile
        st.markdown(
            f"**{p.name}** · {co.ticker} · CIK {p.cik} · FYE {p.fiscal_year_end or '—'} · "
            f"SIC {p.sic or '—'} {p.sic_description or ''}"
        )
    st.caption(
        f"as_of: {ui.fmt_dt(fin.as_of) if fin.as_of else 'latest available'} · policy: "
        f"{fin.availability_policy} · generated {ui.fmt_dt(fin.generated_at)}"
    )
    for w in fin.warnings:
        st.warning(w)

    focus = fin.quarters[-fin.focus_quarters :] if fin.focus_quarters else []
    labels = [p.label for p in focus]
    idx = {p.label: i for i, p in enumerate(fin.quarters)}
    st.subheader("Normalized quarterly values")
    rows = []
    for metric, series in fin.series.items():
        row = {"metric": metric}
        for lab in labels:
            v = series[idx[lab]] if idx[lab] < len(series) else None
            row[lab] = (
                "missing"
                if v is None
                else fmt_value(v.value, v.unit)
                + (" †" if v.method == "derived_ytd_difference" else "")
                + (" (restated)" if v.restated else "")
            )
        rows.append(row)
    ui.table(rows)
    st.caption("† derived from cumulative YTD/annual filings (not directly reported quarterly).")

    st.subheader("Deterministic calculations")
    calc_rows: dict[str, dict] = {}
    for calc in data.calculations:
        if calc.period_label not in labels:
            continue
        row = calc_rows.setdefault(calc.name, {"calculation": calc.label, "formula": calc.formula})
        row[calc.period_label] = (
            fmt_value(calc.value, calc.unit)
            if calc.status == "ok"
            else f"n/a ({'; '.join(calc.warnings)[:60]})"
        )
    ui.table(list(calc_rows.values()))

    st.subheader("Trends (charted from API values)")
    ratios = {
        calc.label: {c2.period_label: c2.value for c2 in data.calculations if c2.name == calc.name}
        for calc in data.calculations
        if calc.name in CHART_RATIOS
    }
    chart = {"period": labels} | {
        name: [None if vals.get(lab) is None else round(vals[lab] * 100, 3) for lab in labels]
        for name, vals in ratios.items()
    }
    if len(chart) > 1 and labels:
        st.markdown("Margins and intensity (%)")
        st.line_chart(chart, x="period")
    rev = fin.series.get("revenue", [])
    fcf = {c2.period_label: c2.value for c2 in data.calculations if c2.name == "free_cash_flow"}
    amounts = {
        "period": labels,
        "Revenue (USD bn)": [
            None if idx[lab] >= len(rev) or rev[idx[lab]] is None else rev[idx[lab]].value / 1e9
            for lab in labels
        ],
        "Free cash flow (USD bn)": [None if fcf.get(lab) is None else fcf[lab] / 1e9 for lab in labels],
    }
    if labels:
        st.markdown("Revenue and free cash flow (USD billions; display scaling only)")
        st.bar_chart(amounts, x="period", stack=False)

    if data.annual_calculations:
        st.subheader("Annual calculations")
        ui.table(
            [
                {
                    "calculation": a.label,
                    "period": a.period_label,
                    "value": fmt_value(a.value, a.unit),
                    "status": a.status,
                }
                for a in data.annual_calculations
            ]
        )

    st.subheader("Missing data")
    if fin.missing:
        st.warning(f"{len(fin.missing)} values are unavailable and were not filled.")
        ui.table([m.model_dump() for m in fin.missing], height=240)
    else:
        st.success("No missing values reported for the selected quarters.")

    with st.expander("Sources (snapshots and per-value filings)"):
        ui.table(
            [
                {
                    "url": s.url,
                    "origin": s.origin,
                    "retrieved_at": ui.fmt_dt(s.retrieved_at),
                    "sha256": s.sha256,
                }
                for s in fin.snapshots
            ],
            link_columns=("url",),
        )
        refs = {
            (s.accession, s.form, str(s.filed), s.filing_url)
            for series in fin.series.values()
            for v in series
            if v
            for s in v.sources
        }
        ui.table(
            [{"accession": a, "form": f, "filed": d, "filing_url": u} for a, f, d, u in sorted(refs)],
            link_columns=("filing_url",),
        )
    ui.json_expander("Raw API response (JSON)", data)


def _report(run: ResearchRunOut) -> None:
    st.markdown(f"**Run** `{run.run_id}` · status **{run.status}** · ticker {run.ticker or '—'}")
    st.caption("Inspect node-by-node execution on the *Agent Execution Inspector* page.")
    if run.error:
        st.error(f"Workflow error: {run.error}")
    rep = run.report
    if rep is None:
        st.warning("No report was persisted for this run.")
        ui.table(run.steps)
        return
    ui.origin_banner(rep.data_origin)
    ui.verification_banner(rep.verification.status, rep.verification.checks)
    llm = rep.llm
    (st.success if llm.status == "generated" else st.info)(
        f"**LLM:** provider `{llm.provider}`, status **{llm.status}**"
        + ("" if llm.status == "generated" else " — deterministic template summary used.")
    )
    for n in llm.notes:
        st.caption(f"LLM note: {n}")

    st.subheader(f"{rep.company_name} ({rep.ticker})")
    st.caption(
        f"as_of {ui.fmt_dt(rep.as_of) if rep.as_of else 'latest'} · periods "
        f"{', '.join(p.label for p in rep.reporting_periods)} · analysed {ui.fmt_dt(rep.analysis_timestamp)}"
    )
    st.markdown("**Executive summary (verified, template)**")
    st.write(rep.executive_summary)
    if rep.model_executive_summary:
        st.markdown("**Model executive summary** — `model_interpretation` (screened, contains no figures)")
        st.write(rep.model_executive_summary)

    st.markdown("**Findings**")
    for f in rep.main_findings:
        st.markdown(
            f"- {f.text}  \n  <small>`{f.kind}` · evidence: {', '.join(f.evidence_ids)}</small>",
            unsafe_allow_html=True,
        )
    if rep.model_interpretations:
        st.markdown(
            "**Model interpretations** — `model_interpretation` (each screened against cited evidence)"
        )
        for f in rep.model_interpretations:
            st.markdown(
                f"- {f.text}  \n  <small>evidence: {', '.join(f.evidence_ids)}</small>",
                unsafe_allow_html=True,
            )

    st.markdown("**Trends**")
    ui.table(
        [
            {
                "id": t.trend_id,
                "metric": t.label,
                "from": f"{t.first_period}: {fmt_value(t.first_value, t.unit)}",
                "to": f"{t.last_period}: {fmt_value(t.last_value, t.unit)}",
                "change": fmt_value(t.change, t.change_unit),
                "direction": t.direction,
                "obs": t.observations,
            }
            for t in rep.trends
        ]
    )
    with st.expander(
        f"Evidence: metric values ({len(rep.metric_values)}) and calculations ({len(rep.derived_calculations)})"
    ):
        ui.table(
            [
                {
                    "id": v.value_id,
                    "metric": v.label,
                    "period": v.period.label,
                    "value": fmt_value(v.value, v.unit),
                    "method": v.method,
                    "concept": v.concept,
                }
                for v in rep.metric_values
            ],
            height=300,
        )
        ui.table(
            [
                {
                    "id": x.calc_id,
                    "calculation": x.label,
                    "period": x.period_label,
                    "value": fmt_value(x.value, x.unit),
                    "formula": x.formula,
                    "status": x.status,
                }
                for x in rep.derived_calculations
            ],
            height=300,
        )
    with st.expander(f"Source filings ({len(rep.source_references)})"):
        ui.table(
            [
                {
                    "id": e.evidence_id,
                    "filing": e.label,
                    "index_url": e.payload.get("index_url"),
                    "document_url": e.payload.get("document_url"),
                }
                for e in rep.source_references
            ],
            link_columns=("index_url", "document_url"),
        )
    if rep.missing_data:
        st.warning(f"{len(rep.missing_data)} missing values (reported, never filled).")
        with st.expander("Missing data"):
            ui.table([m.model_dump() for m in rep.missing_data])
    with st.expander(f"Limitations ({len(rep.limitations)})"):
        for lim in rep.limitations:
            st.markdown(f"- {lim}")
    with st.expander("Report (Markdown)"):
        st.markdown(rep.markdown)
    ui.json_expander("Raw run JSON", run)
