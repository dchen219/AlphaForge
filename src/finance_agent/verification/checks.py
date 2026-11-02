"""Verification layer: references, numeric claims, arithmetic, provenance, as-of leakage."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, Field

from finance_agent.research.trends import Finding
from finance_agent.retrieval.evidence import EvidenceItem, numeric_universe
from finance_agent.timeutil import ensure_aware


class CheckResult(BaseModel):
    name: str
    status: str  # passed | warning | failed
    details: list[str] = Field(default_factory=list)


class VerificationReport(BaseModel):
    status: str  # passed | passed_with_warnings | failed
    checks: list[CheckResult]


# ----------------------------------------------------------------- numeric claims

_STRIP = [
    re.compile(r"\b[A-Z]{1,4}-[0-9a-f]{6,}\b"),  # evidence ids
    re.compile(r"\bF-\d{10}-\d{2}-\d{6}\b"),  # filing evidence ids
    re.compile(r"\b\d{10}-\d{2}-\d{6}\b"),  # accession numbers
    re.compile(r"\bFY\s?\d{4}(?:\s*Q[1-4])?\b"),  # fiscal labels
    re.compile(r"\b(?:Q[1-4]|H[12])\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),  # ISO dates
    re.compile(r"\bFND-\d+\b"),
    re.compile(r"\b(?:10-[KQ]T?|8-K)(?:/A)?\b", re.IGNORECASE),  # form types
    re.compile(r"\b52/53-week\b", re.IGNORECASE),
]
# A bare integer is treated as a count (not a financial figure) only when followed by one of
# these nouns, e.g. "8 quarters", "53-week". Any other unitless number must be supported.
_COUNT_NOUN = re.compile(
    r"\s*-?\s*(?:quarters?|years?|months?|weeks?|days?|periods?|filings?|companies|metrics|"
    r"observations?|segments?)\b",
    re.IGNORECASE,
)
_NUM = re.compile(
    r"(?P<dollar>\$)?(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<dec>\.\d+)?\s*"
    r"(?P<suffix>%|pp\b|percentage points?|percent\b|B\b|bn\b|billion\b|M\b|mn\b|million\b|x\b)?",
    re.IGNORECASE,
)


def _tokens(text: str) -> list[tuple[float, int, str, bool, bool]]:
    """(value, decimals, suffix, has_dollar, is_count) for numeric tokens in ``text``."""
    for pat in _STRIP:
        text = pat.sub(" ", text)
    out = []
    for m in _NUM.finditer(text):
        raw = m.group("int").replace(",", "") + (m.group("dec") or "")
        decimals = len(m.group("dec")) - 1 if m.group("dec") else 0
        suffix = (m.group("suffix") or "").lower()
        is_count = not suffix and decimals == 0 and bool(_COUNT_NOUN.match(text, m.end("int")))
        out.append((float(raw), decimals, suffix, bool(m.group("dollar")), is_count))
    return out


def extract_numbers(text: str) -> list[tuple[float, int, str, bool]]:
    """Return (value, decimals, suffix, has_dollar) for numeric tokens in ``text``."""
    return [t[:4] for t in _tokens(text)]


def _representations(value: float, unit: str, suffix: str, dollar: bool) -> list[float]:
    v = abs(value)
    if suffix in {"%", "percent"}:
        return [v * 100] if unit in {"ratio", "ratio_change"} else []
    if suffix in {"pp", "percentage point", "percentage points"}:
        return [v * 100] if unit == "ratio_change" else []
    if suffix in {"b", "bn", "billion"}:
        return [v / 1e9] if unit == "USD" else []
    if suffix in {"m", "mn", "million"}:
        return [v / 1e6] if unit == "USD" else []
    if suffix == "x":
        return [v] if unit == "multiple" else []
    if dollar:
        return [v] if unit == "USD" else []
    reps = {"USD": [v, v / 1e9, v / 1e6], "ratio": [v * 100, v], "ratio_change": [v * 100], "multiple": [v]}
    return reps.get(unit, [v])


def check_numeric_claim(text: str, universe: list[tuple[float, str]]) -> list[str]:
    """Return a list of unsupported numeric tokens in ``text``."""
    problems = []
    for num, decimals, suffix, dollar, is_count in _tokens(text):
        if not suffix and not dollar and decimals == 0 and (is_count or 1900 <= num <= 2100):
            continue  # explicit counts ("8 quarters") and calendar years
        tol = 0.5 * 10 ** (-decimals) + 1e-9
        ok = any(
            abs(rep - num) <= tol
            for value, unit in universe
            for rep in _representations(value, unit, suffix, dollar)
        )
        if not ok:
            problems.append(f"{'$' if dollar else ''}{num}{suffix}")
    return problems


# ----------------------------------------------------------------- checks


def check_references(findings: list[Finding], items: dict[str, EvidenceItem]) -> CheckResult:
    details = []
    for f in findings:
        if not f.evidence_ids:
            details.append(f"{f.finding_id}: no evidence references.")
        for eid in f.evidence_ids:
            if eid not in items:
                details.append(f"{f.finding_id}: unknown evidence id {eid}.")
    return CheckResult(name="evidence_references", status="failed" if details else "passed", details=details)


def check_numeric_claims(findings: list[Finding], items: dict[str, EvidenceItem]) -> CheckResult:
    details = []
    for f in findings:
        bad = check_numeric_claim(f.text, numeric_universe(items, f.evidence_ids))
        if bad:
            details.append(f"{f.finding_id}: numbers not supported by cited evidence: {', '.join(bad)}")
    return CheckResult(name="numeric_claims", status="failed" if details else "passed", details=details)


_RATIO = {"gross_margin", "operating_margin", "net_margin", "fcf_margin", "capex_to_revenue", "ocf_to_capex"}
_GROWTH = {"revenue_yoy", "revenue_qoq", "capex_yoy", "annual_revenue_yoy"}
_DIFF = {"free_cash_flow", "net_debt", "gross_profit_derived"}


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))


def check_arithmetic(items: dict[str, EvidenceItem]) -> CheckResult:
    """Independently recompute every calculation and every derived fact."""
    details = []
    for it in items.values():
        p = it.payload
        if it.kind == "calculation" and p["status"] == "ok":
            ins = p["inputs"]
            for inp in ins:
                ref = items.get(inp["ref_id"])
                if ref is None or ref.value is None or not _close(ref.value, inp["value"]):
                    details.append(f"{it.evidence_id}: input {inp['name']} does not match {inp['ref_id']}.")
            vals = [i["value"] for i in ins]
            if p["name"] in _RATIO:
                expected = vals[0] / vals[1]
            elif p["name"] in _GROWTH:
                expected = (vals[0] - vals[1]) / abs(vals[1])
            elif p["name"] in _DIFF:
                expected = vals[0] - vals[1]
            else:
                details.append(f"{it.evidence_id}: no recomputation rule for {p['name']}.")
                continue
            if not _close(expected, p["value"]):
                details.append(f"{it.evidence_id}: recomputed {expected} != stored {p['value']}.")
        elif it.kind == "fact":
            src = [s["value"] for s in p["sources"]]
            method = p["method"]
            expected = (
                src[0]
                if method == "reported"
                else src[0] - src[1]
                if method == "derived_ytd_difference"
                else sum(src)
            )
            if not _close(expected, p["value"]):
                details.append(f"{it.evidence_id}: {method} value does not match its sources.")
    return CheckResult(
        name="arithmetic_consistency", status="failed" if details else "passed", details=details
    )


def check_provenance(items: dict[str, EvidenceItem]) -> CheckResult:
    details = []
    for it in items.values():
        if it.kind != "fact":
            continue
        srcs = it.payload.get("sources") or []
        if not srcs:
            details.append(f"{it.evidence_id}: no source facts.")
        for s in srcs:
            for k in ("accession", "filed", "concept", "unit", "period_end", "filing_url"):
                if not s.get(k):
                    details.append(f"{it.evidence_id}: source missing {k}.")
    return CheckResult(name="provenance_complete", status="failed" if details else "passed", details=details)


def check_as_of(items: dict[str, EvidenceItem], as_of: datetime | None) -> CheckResult:
    """Look-ahead guard: no source may become available after the as_of cutoff."""
    if as_of is None:
        return CheckResult(
            name="as_of_compliance",
            status="passed",
            details=["No as_of cutoff requested; latest available data used."],
        )
    cutoff = ensure_aware(as_of)
    details = []
    for it in items.values():
        if it.kind != "fact":
            continue
        for s in it.payload.get("sources") or []:
            avail = ensure_aware(datetime.fromisoformat(s["available_at"]))
            if avail > cutoff:
                details.append(
                    f"{it.evidence_id}: source {s['accession']} available {avail.isoformat()} "
                    f"after as_of {cutoff.isoformat()}."
                )
    return CheckResult(name="as_of_compliance", status="failed" if details else "passed", details=details)


def check_coverage(missing: list[str]) -> CheckResult:
    return CheckResult(name="data_availability", status="warning" if missing else "passed", details=missing)


def summarize(checks: list[CheckResult]) -> VerificationReport:
    if any(c.status == "failed" for c in checks):
        status = "failed"
    elif any(c.status == "warning" for c in checks):
        status = "passed_with_warnings"
    else:
        status = "passed"
    return VerificationReport(status=status, checks=checks)
