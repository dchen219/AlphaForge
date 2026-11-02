"""Human-readable Markdown rendering of a research report."""

from __future__ import annotations

from finance_agent.research.formatting import fmt_value
from finance_agent.schemas.financials import CalculationResult, NormalizedValue
from finance_agent.schemas.research import ResearchReportOut


def origin_banner(origin: str) -> str:
    if origin == "live":
        return "Live data retrieved from official SEC EDGAR APIs."
    if "real_snapshot" in origin and "synthetic" not in origin:
        return (
            "OFFLINE FIXTURE DATA - a saved snapshot of real SEC EDGAR data (see "
            "tests/fixtures/MANIFEST.json for retrieval date). Not refreshed live."
        )
    if "synthetic" in origin:
        return "SYNTHETIC FIXTURE DATA - NOT REAL FINANCIAL DATA. For testing only."
    return f"Data origin: {origin}"


def _cell(v: NormalizedValue | CalculationResult | None) -> str:
    if v is None:
        return "—"
    val = v.value
    if val is None:
        return "n/a"
    s = fmt_value(val, v.unit)
    if isinstance(v, NormalizedValue) and v.method == "derived_ytd_difference":
        s += "†"
    if isinstance(v, NormalizedValue) and v.restated:
        s += "‡"
    return s


def render_markdown(r: ResearchReportOut) -> str:
    labels = [p.label for p in r.reporting_periods]
    facts = {(v.metric, v.period.label): v for v in r.metric_values}
    calcs = {(c.name, c.period_label): c for c in r.derived_calculations}

    def table(rows: list[tuple[str, str, str]]) -> list[str]:
        out = ["| Metric | " + " | ".join(labels) + " |", "|---|" + "---|" * len(labels)]
        for title, kind, key in rows:
            src = facts if kind == "fact" else calcs
            cells = [_cell(src.get((key, lb))) for lb in labels]
            if all(c == "—" for c in cells):
                continue  # metric not requested / not available at all
            out.append(f"| {title} | " + " | ".join(cells) + " |")
        return out

    def findings(section: str) -> list[str]:
        return [
            f"- {f.text} `[{', '.join(f.evidence_ids)}]`" for f in r.main_findings if f.section == section
        ]

    L: list[str] = [f"# {r.company_name} ({r.ticker}) — Company Research Report", ""]
    L.append(f"> **{origin_banner(r.data_origin)}**")
    L += [
        "",
        "| Field | Value |",
        "|---|---|",
        f"| Run ID | `{r.run_id}` |",
        f"| CIK | {r.cik} |",
        f"| Analysis timestamp (UTC) | {r.analysis_timestamp.isoformat()} |",
        f"| As-of cutoff | {r.as_of.isoformat() if r.as_of else 'none (latest available)'} |",
        f"| Periods | {labels[0] if labels else '—'} … {labels[-1] if labels else '—'} ({len(labels)} quarters) |",
        f"| Verification | **{r.verification.status}** |",
        f"| LLM synthesis | {r.llm.status} ({r.llm.provider}) |",
        "",
    ]

    L += ["## 1. Executive summary", "", "**Verified facts (deterministic):**", "", r.executive_summary, ""]
    if r.model_executive_summary:
        L += [
            "**Model-generated interpretation** (Claude; contains no figures, see §5):",
            "",
            r.model_executive_summary,
            "",
        ]

    L += ["## 2. Financial performance", ""]
    L += table(
        [
            ("Revenue", "fact", "revenue"),
            ("Operating income", "fact", "operating_income"),
            ("Net income", "fact", "net_income"),
            ("Operating cash flow", "fact", "operating_cash_flow"),
            ("CAPEX", "fact", "capex"),
            ("Cash & equivalents", "fact", "cash"),
            ("Total debt (excl. leases)", "fact", "total_debt"),
        ]
    )
    L += [
        "",
        "† standalone quarter derived from cumulative (YTD/annual) filings; "
        "‡ value differs across filings (latest eligible filing used).",
        "",
    ]

    L += ["## 3. Growth and margin analysis", ""]
    L += table(
        [
            ("Revenue YoY", "calc", "revenue_yoy"),
            ("Revenue QoQ", "calc", "revenue_qoq"),
            ("Gross margin", "calc", "gross_margin"),
            ("Operating margin", "calc", "operating_margin"),
            ("Net margin", "calc", "net_margin"),
        ]
    )
    L += ["", *findings("growth"), *findings("margins"), ""]

    L += ["## 4. Cash flow and capital expenditures", ""]
    L += table(
        [
            ("Free cash flow", "calc", "free_cash_flow"),
            ("FCF margin", "calc", "fcf_margin"),
            ("CAPEX / revenue", "calc", "capex_to_revenue"),
            ("CAPEX YoY", "calc", "capex_yoy"),
            ("OCF / CAPEX", "calc", "ocf_to_capex"),
            ("Net debt", "calc", "net_debt"),
        ]
    )
    L += ["", *findings("cash_flow"), *findings("capital"), ""]
    L += [
        "*CAPEX definition:* cash purchases of property, plant and equipment "
        "(`PaymentsToAcquirePropertyPlantAndEquipment`, fallback `PaymentsToAcquireProductiveAssets`); "
        "excludes finance-lease principal repayments, non-cash lease additions, acquisitions and "
        "intangibles. FCF = operating cash flow − CAPEX.",
        "",
    ]

    L += ["## 5. Key trends", "", "### Verified findings (computed from source data)", ""]
    L += [
        f"- **{f.finding_id}** {f.text} `[{', '.join(f.evidence_ids)}]`"
        for f in r.main_findings
        if f.section != "summary"
    ] or ["- No trends could be computed."]
    L += ["", "### Model-generated interpretation (not verified fact)", ""]
    if r.model_interpretations:
        L += [
            "*Generated by an LLM from the verified evidence above. Every figure was checked "
            "against the cited evidence; unsupported statements were removed.*",
            "",
        ]
        L += [f"- {f.text} `[{', '.join(f.evidence_ids)}]`" for f in r.model_interpretations]
    else:
        L += [
            f"- LLM synthesis {r.llm.status}. "
            + "; ".join(r.llm.notes)
            + " The report above is a template-based factual summary."
        ]
    L.append("")

    L += ["## 6. Evidence and references", "", "**SEC filings used:**", ""]
    for e in r.source_references:
        p = e.payload
        url = p.get("document_url") or p.get("index_url")
        L.append(
            f"- `{e.evidence_id}` {e.label} — [{p.get('accession')}]({url}) (index: {p.get('index_url')})"
        )
    L += ["", "**Source snapshots (immutable, SHA-256):**", ""]
    for snap in r.snapshots:
        L.append(
            f"- {snap.url} — retrieved {snap.retrieved_at.isoformat()} — origin `{snap.origin}` — "
            f"`{snap.sha256[:16]}…`"
        )
    L += [
        "",
        "Every figure maps to an evidence id in the JSON report: `V-…` normalized XBRL values "
        "(with accession, concept, unit, period and filing date), `C-…` calculations (formula + "
        "inputs), `T-…` trend statistics.",
        "",
    ]

    L += ["## 7. Data limitations", ""]
    L += [f"- {x}" for x in r.limitations] or ["- None recorded."]
    L += ["", "### Verification checks", "", "| Check | Status | Details |", "|---|---|---|"]
    for c in r.verification.checks:
        detail = "; ".join(c.details[:3]) + (f" (+{len(c.details) - 3} more)" if len(c.details) > 3 else "")
        L.append(f"| {c.name} | {c.status} | {detail or '—'} |")
    L.append("")
    return "\n".join(L)
