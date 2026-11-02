"""Daily intelligence briefing: entry construction, deterministic text, verification, rendering."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from finance_agent.events.importance import RULE_CONSTANTS
from finance_agent.events.schemas import EventCategory, FinancialEvent, Unit
from finance_agent.timeutil import ET
from finance_agent.verification.checks import CheckResult, VerificationReport, check_numeric_claim

MACRO_TYPES = {EventCategory.MACRO_RELEASE, EventCategory.CENTRAL_BANK}


class BriefingRequest(BaseModel):
    date: str | None = Field(
        default=None, description="YYYY-MM-DD (New York). Cutoff = 23:59:59 ET that day."
    )
    as_of: datetime | None = Field(
        default=None, description="Explicit timezone-aware cutoff (overrides date)."
    )
    window_hours: int | None = Field(
        default=None, ge=1, le=24 * 31, description="Look-back window; default = the New York calendar day."
    )
    max_macro: int = Field(default=6, ge=1, le=25)
    max_corporate: int = Field(default=8, ge=0, le=50)
    tickers: list[str] | None = None
    use_llm: bool = True


class BriefingEntry(BaseModel):
    rank: int
    section: str  # macro | corporate
    cluster_id: str
    primary_event_id: str
    event_ids: list[str]
    category: str
    subtype: str
    title: str
    what_happened: str
    when: str
    what_was_reported: str
    what_changed: list[str]
    why_it_matters: list[str]  # economic hypotheses, labelled
    affected: list[str]
    importance_score: float | None
    importance_level: str | None
    importance_explanation: list[str]
    evidence_ids: list[str]
    source_urls: list[str]
    related: list[str]
    data_origin: str
    notes: list[str] = Field(default_factory=list)


class BriefingReport(BaseModel):
    briefing_id: str
    status: str
    as_of: datetime
    window_start: datetime
    window_end: datetime
    generated_at: datetime
    data_origins: list[str]
    executive_summary: str
    model_executive_summary: str | None = None
    model_interpretations: list[dict] = Field(default_factory=list)
    macro_entries: list[BriefingEntry]
    corporate_entries: list[BriefingEntry]
    implications: list[str]
    affected_markets: list[str]
    upcoming: list[dict]
    uncertainty: list[str]
    sources: list[dict]
    verification: VerificationReport
    llm: dict
    counts: dict[str, int]
    markdown: str = ""


# ------------------------------------------------------------------ formatting


def fmt(value: float | None, unit: str | Unit | None, signed: bool = False) -> str:
    if value is None:
        return "n/a"
    u = str(unit) if unit else ""
    sign = "+" if signed and value > 0 else ""
    if u == Unit.PERCENT_CHANGE:
        return f"{sign}{value:.2f}%"
    if u == Unit.PERCENT:
        return f"{sign}{value:.2f}%"
    if u == Unit.PERCENTAGE_POINTS:
        return f"{'+' if value > 0 else ''}{value:.2f} pp"
    if u == Unit.THOUSANDS_OF_PERSONS:
        return f"{sign}{value:,.0f} thousand persons"
    if u == Unit.BASIS_POINTS:
        return f"{'+' if value > 0 else ''}{value:.0f} bp"
    if u == Unit.INDEX_POINTS:
        return f"{sign}{value:,.3f} index points"
    if u == Unit.BILLIONS_USD:
        return f"{sign}${value:,.1f} billion"
    if u == Unit.COUNT:
        return f"{value:.2f}"
    return f"{value:,.4g}"


def universe_entries(value: float | None, unit: str | Unit | None) -> list[tuple[float, str]]:
    """Map an event figure into the verifier's (value, unit) convention."""
    if value is None:
        return []
    u = str(unit) if unit else ""
    if u in {Unit.PERCENT_CHANGE, Unit.PERCENT}:
        return [(value / 100, "ratio")]
    if u == Unit.PERCENTAGE_POINTS:
        return [(value / 100, "ratio_change")]
    if u == Unit.BILLIONS_USD:
        return [(value * 1e9, "USD")]
    return [(value, "count")]


def event_universe(ev: FinancialEvent) -> list[tuple[float, str]]:
    out = universe_entries(ev.value, ev.unit) + universe_entries(ev.previous_value, ev.unit)
    for c in ev.comparisons:
        out += universe_entries(c.value, c.unit)
        for inp in c.inputs:
            if isinstance(inp.get("value"), int | float):
                out += [(float(inp["value"]), "count"), *universe_entries(float(inp["value"]), c.unit)]
    for k in ("target_range_lower", "target_range_upper"):
        if isinstance(ev.attributes.get(k), int | float):
            out += universe_entries(ev.attributes[k], Unit.PERCENT)
    if isinstance(ev.attributes.get("level"), int | float):
        out.append((float(ev.attributes["level"]), "count"))
    if ev.importance_score is not None:
        out.append((ev.importance_score, "count"))
        out += [(float(k), "count") for k in RULE_CONSTANTS]
    for comp in (ev.importance_components or {}).values():
        if isinstance(comp, dict) and isinstance(comp.get("value"), int | float):
            out += [(comp["value"], "count"), (comp["max"], "count")]
    return out


def when_text(ev: FinancialEvent) -> str:
    t = ev.available_at.astimezone(ET).isoformat() if ev.available_at else "unknown"
    basis = {
        "EXACT_TIMESTAMP": "exact source timestamp",
        "SCHEDULED_TIME_CONFIRMED_DATE": "official scheduled time; publication date confirmed by vintage data",
        "DATE_ONLY_CONSERVATIVE": "date-only source; conservative next-day availability",
        "UNKNOWN": "availability unknown",
    }.get(str(ev.availability_precision), str(ev.availability_precision))
    return f"Available {t} ({basis})."


def describe(ev: FinancialEvent) -> tuple[str, list[str]]:
    """(what was reported, what changed) — every number comes from stored event data."""
    rep = ev.description or ev.title
    changes: list[str] = []
    if ev.event_type == EventCategory.MACRO_RELEASE and ev.value is not None:
        rep = f"{ev.title}: headline {fmt(ev.value, ev.unit, signed=True)}"
        if ev.previous_value is not None:
            rep += f" (previous period {fmt(ev.previous_value, ev.unit, signed=True)})"
        rep += "."
    elif ev.event_type == EventCategory.CENTRAL_BANK and ev.value is not None:
        lo, hi = ev.attributes.get("target_range_lower"), ev.attributes.get("target_range_upper")
        rep = f"Target range for the federal funds rate: {fmt(lo, Unit.PERCENT)}–{fmt(hi, Unit.PERCENT)}."
    elif ev.event_subtype == "FINANCIAL_METRIC_CHANGE" and ev.value is not None:
        rep = f"Revenue growth YoY {fmt(ev.value, ev.unit, signed=True)}"
        if ev.previous_value is not None:
            rep += f" (prior quarter {fmt(ev.previous_value, ev.unit, signed=True)})"
        rep += "."
    for c in ev.comparisons:
        if c.status != "ok" or c.value is None:
            if c.note:
                changes.append(f"{c.label}: not computed — {c.note}")
            continue
        if c.kind in {"percentile"}:
            changes.append(f"{c.label}: {c.value:.1f}")
        elif c.kind == "standardized_change":
            changes.append(f"{c.label}: {c.value:+.2f} standard deviations")
        else:
            changes.append(f"{c.label}: {fmt(c.value, c.unit, signed=True)}")
    if ev.event_type == EventCategory.MACRO_RELEASE:
        changes.append(
            "Market consensus: unavailable (no authenticated consensus source); no surprise computed."
        )
    return rep, changes


def build_entry(
    rank: int, section: str, cluster_id: str, members: list[FinancialEvent], sources: dict[str, list[dict]]
) -> BriefingEntry:
    primary = members[0]
    reported, changed = describe(primary)
    for m in members[1:]:
        r, c = describe(m)
        reported += f" Also: {r}"
        changed += [f"[{m.event_subtype}] {x}" for x in c if "consensus" not in x]
    why = [
        f"Hypothesis ({ch.interpretation_type.replace('_', ' ').lower()}): {ch.source_variable} → "
        f"{ch.target_variable}: {ch.direction}. {ch.mechanism}"
        for ch in primary.channels
    ]
    comps = primary.importance_components or {}
    expl = [
        f"{k}: {v['value']} / {v['max']} — {v['explanation']}"
        if v.get("available")
        else f"{k}: not available — {v.get('explanation', '')}"
        for k, v in comps.items()
        if isinstance(v, dict)
    ]
    urls = sorted(
        {s["source_url"] for m in members for s in sources.get(m.event_id, []) if s.get("source_url")}
        | {m.source_url for m in members if m.source_url}
    )
    affected = sorted({*primary.affected_entities, *primary.affected_industries})
    notes = []
    if primary.revision_status == "REVISION":
        notes.append(
            "This is a revision of previously published data, not new information about the latest period."
        )
    if primary.availability_precision != "EXACT_TIMESTAMP":
        notes.append(when_text(primary))
    return BriefingEntry(
        rank=rank,
        section=section,
        cluster_id=cluster_id,
        primary_event_id=primary.event_id,
        event_ids=[m.event_id for m in members],
        category=str(primary.event_type),
        subtype=str(primary.event_subtype),
        title=primary.title,
        what_happened=primary.title + ".",
        when=when_text(primary),
        what_was_reported=reported,
        what_changed=changed,
        why_it_matters=why,
        affected=affected,
        importance_score=primary.importance_score,
        importance_level=str(primary.importance_level),
        importance_explanation=expl,
        evidence_ids=[m.event_id for m in members] + [c.comparison_id for c in primary.comparisons],
        source_urls=urls,
        related=[f"{m.event_subtype}: {m.title}" for m in members[1:]],
        data_origin=",".join(sorted({m.data_origin for m in members})),
        notes=notes,
    )


def verify_entries(
    entries: list[BriefingEntry], events: dict[str, FinancialEvent], as_of: datetime, window_start: datetime
) -> list[CheckResult]:
    num_problems, ref_problems, time_problems, status_problems = [], [], [], []
    for e in entries:
        universe = [x for eid in e.event_ids if eid in events for x in event_universe(events[eid])]
        for field_name in ("what_was_reported", "what_happened"):
            bad = check_numeric_claim(getattr(e, field_name), universe)
            if bad:
                num_problems.append(f"rank {e.rank} {field_name}: unsupported {bad}")
        for line in e.what_changed + e.importance_explanation:
            bad = check_numeric_claim(line, universe)
            if bad:
                num_problems.append(f"rank {e.rank}: unsupported {bad} in {line[:60]!r}")
        for eid in e.event_ids:
            ev = events.get(eid)
            if ev is None:
                ref_problems.append(f"rank {e.rank}: unknown event {eid}")
                continue
            if not ev.has_occurred:
                status_problems.append(f"{eid}: {ev.event_status} event presented as occurred")
            if ev.available_at is None or ev.available_at > as_of:
                time_problems.append(f"{eid}: available_at {ev.available_at} after cutoff {as_of}")
            elif ev.available_at <= window_start:
                time_problems.append(f"{eid}: available_at {ev.available_at} before window start")
        if not e.source_urls:
            ref_problems.append(f"rank {e.rank}: no source URL")
    return [
        CheckResult(
            name="numeric_claims", status="failed" if num_problems else "passed", details=num_problems
        ),
        CheckResult(
            name="evidence_references", status="failed" if ref_problems else "passed", details=ref_problems
        ),
        CheckResult(
            name="as_of_compliance", status="failed" if time_problems else "passed", details=time_problems
        ),
        CheckResult(
            name="occurrence_status",
            status="failed" if status_problems else "passed",
            details=status_problems or ["No scheduled/unconfirmed event is presented as having occurred."],
        ),
    ]


# ------------------------------------------------------------------ markdown


def origin_banner(origins: list[str]) -> str:
    if not origins:
        return "No events in window."
    if all(o == "live" for o in origins):
        return "Live data retrieved from official sources (SEC EDGAR, Federal Reserve, BLS/BEA via ALFRED)."
    if any("synthetic" in o for o in origins):
        return "CONTAINS SYNTHETIC FIXTURE DATA - NOT REAL. For testing only."
    if all(o.startswith("fixture:real_snapshot") for o in origins):
        return (
            "OFFLINE REPLAY of saved REAL official source snapshots (see tests/fixtures/MANIFEST.json). "
            "Not refreshed live."
        )
    return f"Data origins: {', '.join(origins)}"


def render_markdown(r: BriefingReport) -> str:
    L = [
        f"# AlphaForge Daily Intelligence Briefing — {r.as_of.astimezone(ET).date().isoformat()}",
        "",
        f"> **{origin_banner(r.data_origins)}**",
        "",
        "## A. Date and coverage",
        "",
        f"- Cutoff (as_of): {r.as_of.astimezone(ET).isoformat()} (New York) / {r.as_of.isoformat()}",
        f"- Window: {r.window_start.astimezone(ET).isoformat()} → {r.window_end.astimezone(ET).isoformat()}",
        f"- Briefing ID: `{r.briefing_id}` · Verification: **{r.verification.status}** · "
        f"LLM: {r.llm.get('status')} ({r.llm.get('provider')})",
        "",
        "## B. Executive summary",
        "",
        "**Verified (deterministic):** " + r.executive_summary,
        "",
    ]
    if r.model_executive_summary:
        L += [
            "**Model-generated interpretation** (contains no figures; not verified fact):",
            "",
            r.model_executive_summary,
            "",
        ]

    def section(title: str, entries: list[BriefingEntry]) -> None:
        L.extend([f"## {title}", ""])
        if not entries:
            L.extend(["- No verified events in this window.", ""])
            return
        for e in entries:
            L.append(f"### {e.rank}. {e.title}  — importance {e.importance_level} ({e.importance_score})")
            L.append("")
            L.append(f"- **What happened:** {e.what_happened}")
            L.append(f"- **When:** {e.when}")
            L.append(f"- **What was reported:** {e.what_was_reported}")
            if e.what_changed:
                L.append("- **What changed:**")
                L.extend(f"  - {c}" for c in e.what_changed)
            if e.why_it_matters:
                L.append("- **Why it might matter** (economic hypotheses — not observed market reactions):")
                L.extend(f"  - {w}" for w in e.why_it_matters)
            L.append(f"- **Possibly affected:** {', '.join(e.affected) or 'n/a'}")
            if e.related:
                L.append(f"- **Same event cluster (counted once):** {'; '.join(e.related)}")
            L.append(f"- **Evidence:** `{', '.join(e.event_ids)}` · " + " · ".join(e.source_urls[:3]))
            L.append("- **Why this rank:** " + "; ".join(e.importance_explanation[:6]))
            for n in e.notes:
                L.append(f"- _Note:_ {n}")
            L.append("")

    section("C. Top consequential macroeconomic events", r.macro_entries)
    section("D. Important corporate disclosures", r.corporate_entries)
    L += (
        [
            "## E. Economic implications",
            "",
            "*Evidence-linked hypotheses; direction only. No causal estimates, "
            "probabilities or price impacts.*",
            "",
        ]
        + ([f"- {x}" for x in r.implications] or ["- None."])
        + [""]
    )
    L += (
        ["## F. Possibly affected markets and asset classes", ""]
        + ([f"- {x}" for x in r.affected_markets] or ["- None identified."])
        + [""]
    )
    L += ["## G. Uncertainty and missing information", ""] + [f"- {x}" for x in r.uncertainty] + [""]
    if r.upcoming:
        L += ["### Upcoming scheduled releases (calendar only — NOT events that occurred)", ""]
        L += [f"- {u['scheduled_for']}: {u['title']}" for u in r.upcoming] + [""]
    L += ["## H. Sources", ""]
    L += [
        f"- `{s['event_id']}` {s['source_name']} ({s['role']}): {s.get('source_url') or 'n/a'} — origin "
        f"`{s.get('data_origin')}`"
        + (f" — snapshot `{s['snapshot_sha256'][:12]}…`" if s.get("snapshot_sha256") else "")
        for s in r.sources
    ] or ["- None."]
    L += ["", "### Verification checks", "", "| Check | Status | Details |", "|---|---|---|"]
    for c in r.verification.checks:
        d = "; ".join(c.details[:3]) + (f" (+{len(c.details) - 3} more)" if len(c.details) > 3 else "")
        L.append(f"| {c.name} | {c.status} | {d or '—'} |")
    return "\n".join(L) + "\n"


def summarize_counts(macro: list[BriefingEntry], corp: list[BriefingEntry]) -> dict[str, Any]:
    return {
        "macro_clusters": len(macro),
        "corporate_clusters": len(corp),
        "events": sum(len(e.event_ids) for e in macro + corp),
    }
