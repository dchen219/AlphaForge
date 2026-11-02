"""Company Research Agent: request interpretation and evidence-bound synthesis."""

from __future__ import annotations

import logging
import re
from typing import Any

from pydantic import BaseModel, Field

from finance_agent.agents.llm import LLMProvider, LLMUnavailableError
from finance_agent.research.formatting import fmt_value
from finance_agent.research.trends import Finding, TrendResult
from finance_agent.retrieval.evidence import EvidenceItem
from finance_agent.schemas.sec import TickerEntry

log = logging.getLogger(__name__)

METRIC_GROUPS: dict[str, list[str]] = {
    "revenue_growth": ["revenue_yoy", "revenue_qoq", "annual_revenue_yoy"],
    "margins": ["gross_margin", "operating_margin", "net_margin"],
    "free_cash_flow": ["free_cash_flow", "fcf_margin"],
    "capex_intensity": ["capex_to_revenue", "capex_yoy", "ocf_to_capex"],
    "capital_structure": ["net_debt"],
}
_KEYWORDS = {
    "revenue_growth": ["revenue", "sales", "top line", "growth"],
    "margins": ["margin", "profitab", "gross", "operating income", "net income", "earnings"],
    "free_cash_flow": ["free cash flow", "fcf", "cash flow"],
    "capex_intensity": [
        "capex",
        "capital expenditure",
        "capital intensity",
        "investment",
        "capital spending",
    ],
    "capital_structure": ["debt", "leverage", "balance sheet", "net debt", "cash position"],
}
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "sixteen": 16,
    "twenty": 20,
}
_STOP_TICKERS = {
    "A",
    "I",
    "AND",
    "THE",
    "FOR",
    "OF",
    "OVER",
    "MOST",
    "CEO",
    "CFO",
    "USD",
    "YOY",
    "QOQ",
    "FCF",
    "GAAP",
    "SEC",
    "EPS",
    "AI",
    "IT",
    "ON",
    "IN",
    "BY",
    "AT",
    "TO",
    "OR",
}


class ParsedRequest(BaseModel):
    ticker: str | None
    quarters: int
    metric_groups: list[str]
    method: str  # deterministic | llm | explicit
    notes: list[str] = Field(default_factory=list)


def _name_index(ticker_map: dict[str, TickerEntry]) -> dict[str, str]:
    """Lower-cased first word(s) of company names → ticker (only unambiguous ones)."""
    idx: dict[str, set[str]] = {}
    for t, e in ticker_map.items():
        base = re.sub(r"[^a-z0-9 ]", " ", e.title.lower()).split()
        if base:
            idx.setdefault(base[0], set()).add(t)
    return {k: next(iter(v)) for k, v in idx.items() if len(v) == 1 and len(k) >= 4}


ALIASES = {
    "microsoft": "MSFT",
    "apple": "AAPL",
    "amazon": "AMZN",
    "alphabet": "GOOGL",
    "google": "GOOGL",
    "meta": "META",
    "facebook": "META",
}


def parse_request_deterministic(
    question: str, ticker_map: dict[str, TickerEntry], default_quarters: int = 8
) -> ParsedRequest:
    notes: list[str] = []
    ticker = None
    for tok in re.findall(r"\$?\b[A-Z][A-Z.]{0,5}\b", question):
        t = tok.lstrip("$")
        if t in ticker_map and t not in _STOP_TICKERS:
            ticker = t
            break
    if ticker is None:
        lowered = question.lower()
        words = re.findall(r"[a-z0-9]+", lowered.replace("'s", ""))
        for w in words:
            if w in ALIASES and ALIASES[w] in ticker_map:
                ticker = ALIASES[w]
                break
        if ticker is None:
            idx = _name_index(ticker_map)
            ticker = next((idx[w] for w in words if w in idx), None)
        if ticker:
            notes.append(f"Company name in question resolved to ticker {ticker}.")
    quarters = default_quarters
    m = re.search(
        r"\b(\d{1,2}|" + "|".join(_NUMBER_WORDS) + r")\s+(?:most\s+recent\s+)?"
        r"(?:reported\s+)?(?:fiscal\s+)?quarters?\b",
        question.lower(),
    )
    if m:
        raw = m.group(1)
        quarters = int(raw) if raw.isdigit() else _NUMBER_WORDS[raw]
    groups = [g for g, kws in _KEYWORDS.items() if any(k in question.lower() for k in kws)]
    if not groups:
        groups = list(METRIC_GROUPS)
        notes.append("No specific metrics recognised; analysing all supported metric groups.")
    return ParsedRequest(
        ticker=ticker,
        quarters=max(2, min(quarters, 20)),
        metric_groups=groups,
        method="deterministic",
        notes=notes,
    )


PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "ticker": {"type": ["string", "null"], "description": "US stock ticker, uppercase"},
        "quarters": {"type": ["integer", "null"]},
        "metric_groups": {"type": "array", "items": {"type": "string", "enum": list(METRIC_GROUPS)}},
    },
    "required": ["ticker", "quarters", "metric_groups"],
    "additionalProperties": False,
}


def parse_request(
    question: str, ticker_map: dict[str, TickerEntry], llm: LLMProvider, default_quarters: int = 8
) -> ParsedRequest:
    parsed = parse_request_deterministic(question, ticker_map, default_quarters)
    if parsed.ticker or not llm.available:
        return parsed
    try:
        out = llm.complete_json(
            system="You extract structured parameters from financial research requests. "
            "Return only what the request states; use null when unknown.",
            user=f"Request: {question}",
            schema=PARSE_SCHEMA,
            max_tokens=2000,
        )
    except LLMUnavailableError as exc:
        parsed.notes.append(f"LLM request interpretation unavailable: {exc}")
        return parsed
    t = (out.get("ticker") or "").upper() or None
    if t and t not in ticker_map:
        parsed.notes.append(f"LLM proposed ticker {t}, which is not an SEC filer; ignored.")
        t = None
    return ParsedRequest(
        ticker=t,
        quarters=max(2, min(int(out.get("quarters") or parsed.quarters), 20)),
        metric_groups=[g for g in out.get("metric_groups", []) if g in METRIC_GROUPS] or parsed.metric_groups,
        method="llm",
        notes=[*parsed.notes, "Ticker identified by LLM and validated against SEC map."],
    )


# ------------------------------------------------------------------ synthesis

SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "executive_summary": {"type": "string"},
        "interpretations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "evidence_ids"],
                "additionalProperties": False,
            },
        },
        "limitations": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["executive_summary", "interpretations", "limitations"],
    "additionalProperties": False,
}

SYNTH_SYSTEM = """You are a financial research analyst writing interpretation for a report \
whose numbers were computed deterministically from SEC XBRL filings.

Rules:
- Use ONLY the evidence provided. Never introduce a number that is not shown in the evidence, \
and copy numbers exactly as displayed (same rounding and units, e.g. "$77.67B", "45.2%", "+1.3 pp").
- Every interpretation must cite the evidence ids (e.g. "T-…", "C-…") that support it.
- The executive summary must contain NO numbers at all; describe direction and significance only.
- Interpret trends (what changed, plausible drivers framed as hypotheses, what to monitor). \
Mark speculation as such. Do not predict stock prices or give investment advice.
- Note data limitations you see (missing metrics, derived quarters, restatements, concept changes)."""


def build_synthesis_prompt(
    company: str,
    ticker: str,
    trends: list[TrendResult],
    items: dict[str, EvidenceItem],
    focus_labels: list[str],
    warnings: list[str],
) -> str:
    lines = [
        f"Company: {company} ({ticker})",
        f"Quarters analysed: {', '.join(focus_labels)}",
        "",
        "TRENDS (id | metric | first -> last | change | min | max):",
    ]
    for t in trends:
        lines.append(
            f"{t.trend_id} | {t.label} | {fmt_value(t.first_value, t.unit)} ({t.first_period}) -> "
            f"{fmt_value(t.last_value, t.unit)} ({t.last_period}) | "
            f"{fmt_value(t.change, t.change_unit)} | {fmt_value(t.min_value, t.unit)} ({t.min_period}) | "
            f"{fmt_value(t.max_value, t.unit)} ({t.max_period})"
        )
    lines += ["", "QUARTERLY CALCULATIONS (id | metric | period | value):"]
    for it in items.values():
        if it.kind == "calculation" and it.value is not None and it.period in focus_labels:
            lines.append(f"{it.evidence_id} | {it.label} | {it.period} | {it.display}")
    if warnings:
        lines += ["", "DATA WARNINGS:"] + [f"- {w}" for w in warnings[:25]]
    return "\n".join(lines)


class Synthesis(BaseModel):
    executive_summary: str | None
    interpretations: list[Finding]
    limitations: list[str]
    provider: str
    status: str  # generated | unavailable | failed
    dropped: list[str] = Field(default_factory=list)


def synthesize(llm: LLMProvider, prompt: str) -> Synthesis:
    if not llm.available:
        reason = getattr(llm, "reason", "LLM unavailable")
        return Synthesis(
            executive_summary=None,
            interpretations=[],
            limitations=[],
            provider=llm.name,
            status="unavailable",
            dropped=[reason],
        )
    try:
        out: dict[str, Any] = llm.complete_json(system=SYNTH_SYSTEM, user=prompt, schema=SYNTH_SCHEMA)
    except LLMUnavailableError as exc:
        return Synthesis(
            executive_summary=None,
            interpretations=[],
            limitations=[],
            provider=llm.name,
            status="failed",
            dropped=[str(exc)],
        )
    interps = [
        Finding(
            finding_id=f"INT-{i + 1}",
            section="interpretation",
            text=str(x.get("text", "")),
            evidence_ids=[str(e) for e in x.get("evidence_ids", [])],
            kind="model_interpretation",
        )
        for i, x in enumerate(out.get("interpretations", []))
    ]
    return Synthesis(
        executive_summary=str(out.get("executive_summary") or "") or None,
        interpretations=interps,
        limitations=[str(x) for x in out.get("limitations", [])],
        provider=llm.name,
        status="generated",
    )
