"""Bounded daily-briefing workflow (LangGraph, linear, no self-loops).

parse → window → retrieve → resolve → compare → rank → select → evidence → explain
→ verify → persist        (any node error → fail, recorded on the briefing)

Works fully without an LLM: the deterministic template narrative is always produced.
"""

from __future__ import annotations

import itertools
import logging
import time
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from datetime import time as dtime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from finance_agent.agents.llm import LLMUnavailableError
from finance_agent.errors import DataUnavailableError, FinanceAgentError
from finance_agent.events.briefing import (
    MACRO_TYPES,
    BriefingEntry,
    BriefingReport,
    BriefingRequest,
    build_entry,
    event_universe,
    render_markdown,
    summarize_counts,
    verify_entries,
)
from finance_agent.events.importance import apply_importance
from finance_agent.events.schemas import FinancialEvent
from finance_agent.events.store import BriefingStore, EventStore
from finance_agent.events.transmission import channels_for
from finance_agent.timeutil import ET, ensure_aware, parse_as_of, utcnow
from finance_agent.verification.checks import CheckResult, check_numeric_claim, summarize

log = logging.getLogger(__name__)

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
                    "event_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "event_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["executive_summary", "interpretations"],
    "additionalProperties": False,
}
SYNTH_SYSTEM = """You write interpretation for a daily financial intelligence briefing whose facts were
extracted deterministically from official sources (SEC EDGAR, Federal Reserve, BLS/BEA via ALFRED).
Rules: use only the provided events; copy any number exactly as displayed; cite event ids for every
interpretation; the executive summary must contain NO numbers; label mechanisms as hypotheses; never
claim an observed market reaction, price move, or consensus surprise (none are available); no advice."""


class BriefingState(TypedDict, total=False):
    briefing_id: str
    request: BriefingRequest
    as_of: datetime
    window_start: datetime
    events: list[FinancialEvent]
    clusters: dict[str, list[FinancialEvent]]
    upcoming: list[FinancialEvent]
    unconfirmed: list[FinancialEvent]
    macro: list[tuple[str, list[FinancialEvent]]]
    corporate: list[tuple[str, list[FinancialEvent]]]
    sources: dict[str, list[dict]]
    entries: tuple[list[BriefingEntry], list[BriefingEntry]]
    narrative: dict[str, Any]
    verification: Any
    report: BriefingReport
    steps: list[dict]
    error: str | None


def resolve_window(req: BriefingRequest) -> tuple[datetime, datetime]:
    """Explicit timezone-aware cutoff. Date-only = end of that New York day (v0.1 convention)."""
    if req.as_of is not None:
        as_of = ensure_aware(req.as_of)
    elif req.date:
        as_of = parse_as_of(req.date)
    else:
        as_of = utcnow()
    if req.window_hours:
        start = as_of - timedelta(hours=req.window_hours)
    else:
        start = datetime.combine(as_of.astimezone(ET).date(), dtime(0), tzinfo=ET)
    return as_of, start


class BriefingWorkflow:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.store = EventStore(ctx.db)
        self.briefings = BriefingStore(ctx.db)
        self.graph = self._build()

    def _build(self):
        g = StateGraph(BriefingState)
        order = [
            "parse",
            "window",
            "retrieve",
            "resolve",
            "compare",
            "rank",
            "select",
            "evidence",
            "explain",
            "verify",
            "persist",
        ]
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

    def _wrap(self, name, fn):
        def node(state: BriefingState) -> dict:
            t0 = time.perf_counter()
            steps = list(state.get("steps", []))
            try:
                update = fn(state) or {}
                steps.append(
                    {"step": name, "status": "ok", "ms": round((time.perf_counter() - t0) * 1000, 1)}
                )
            except FinanceAgentError as exc:
                update = {"error": f"{name}: {exc}"}
                steps.append({"step": name, "status": "error", "error": str(exc)})
            except Exception as exc:  # defects are recorded, never masked
                log.exception("briefing step crashed", extra={"step": name})
                update = {"error": f"{name}: unexpected {type(exc).__name__}: {exc}"}
                steps.append({"step": name, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            update["steps"] = steps
            return update

        return node

    def run(self, req: BriefingRequest) -> BriefingState:
        bid = str(uuid.uuid4())
        return self.graph.invoke({"briefing_id": bid, "request": req, "steps": [], "error": None})

    # ---------------------------------------------------------------- nodes
    def parse(self, s):
        req = s["request"]
        if req.tickers:
            req.tickers = [t.upper() for t in req.tickers]
        return {}

    def window(self, s):
        as_of, start = resolve_window(s["request"])
        if start >= as_of:
            raise DataUnavailableError("Empty briefing window.")
        self.briefings.create(s["briefing_id"], as_of, start, s["request"].model_dump(mode="json"))
        return {"as_of": as_of, "window_start": start}

    def retrieve(self, s):
        as_of, start = s["as_of"], s["window_start"]
        events = self.store.query(as_of=as_of, start=start, end=as_of, limit=2000)
        if s["request"].tickers:
            t = set(s["request"].tickers)
            events = [e for e in events if e.event_type in MACRO_TYPES or set(e.affected_entities) & t]
        upcoming = self.store.calendar(start=as_of, end=as_of + timedelta(days=7))
        due = self.store.calendar(start=start, end=as_of)
        unconfirmed = []
        for d in due:  # scheduled within window: occurred only if a linked publication is available by as_of
            linked = [e for e in events if e.related_event_id == d.event_id]
            if not linked:
                unconfirmed.append(d)
        return {"events": events, "upcoming": upcoming, "unconfirmed": unconfirmed}

    def resolve(self, s):
        clusters: dict[str, list[FinancialEvent]] = defaultdict(list)
        for e in s["events"]:
            clusters[e.event_cluster_id or f"SINGLE-{e.event_id}"].append(e)
        return {"clusters": dict(clusters)}

    def compare(self, s):
        # Importance and channels are pure functions of each event's own (point-in-time) data.
        for members in s["clusters"].values():
            for e in members:
                apply_importance(e)
                e.channels = channels_for(e)
        return {}

    def rank(self, s):
        macro, corp = [], []

        def key(item):
            cid, members = item
            top = members[0]
            return (-(top.importance_score or -1), top.available_at, cid)

        for cid, members in s["clusters"].items():
            # Primary = highest-scoring member; a cluster is ranked once (no repetition bonus).
            members.sort(key=lambda e: (-(e.importance_score or -1), e.available_at, e.event_id))
            (macro if members[0].event_type in MACRO_TYPES else corp).append((cid, members))
        return {"macro": sorted(macro, key=key), "corporate": sorted(corp, key=key)}

    def select(self, s):
        req = s["request"]
        return {"macro": s["macro"][: req.max_macro], "corporate": s["corporate"][: req.max_corporate]}

    def evidence(self, s):
        out: dict[str, list[dict]] = {}
        for _, members in s["macro"] + s["corporate"]:
            for e in members:
                out[e.event_id] = [
                    {
                        "event_id": e.event_id,
                        "source_name": r.source_name,
                        "role": r.role,
                        "source_url": r.source_url,
                        "snapshot_sha256": r.snapshot_sha256,
                        "retrieved_at": r.retrieved_at.isoformat() if r.retrieved_at else None,
                        "excerpt": r.excerpt,
                        "data_origin": e.data_origin,
                    }
                    for r in self.store.sources(e.event_id)
                ]
        return {"sources": out}

    def explain(self, s):
        macro = [build_entry(i + 1, "macro", cid, m, s["sources"]) for i, (cid, m) in enumerate(s["macro"])]
        corp = [
            build_entry(i + 1, "corporate", cid, m, s["sources"]) for i, (cid, m) in enumerate(s["corporate"])
        ]
        narrative = {
            "status": "unavailable",
            "provider": "none",
            "notes": [],
            "summary": None,
            "interpretations": [],
        }
        req = s["request"]
        llm = self.ctx.llm
        if not req.use_llm:
            narrative["notes"].append("LLM synthesis disabled for this request.")
        elif not llm.available:
            narrative.update(provider=llm.name, notes=[getattr(llm, "reason", "LLM unavailable")])
        elif macro or corp:
            prompt = "\n".join(
                f"{e.primary_event_id} | {e.title} | {e.what_was_reported} | changes: "
                f"{'; '.join(e.what_changed[:4])}"
                for e in macro + corp
            )
            try:
                out = llm.complete_json(
                    system=SYNTH_SYSTEM, user=prompt, schema=SYNTH_SCHEMA, max_tokens=8000
                )
                narrative.update(
                    status="generated",
                    provider=llm.name,
                    summary=out.get("executive_summary"),
                    interpretations=out.get("interpretations", []),
                )
            except LLMUnavailableError as exc:
                narrative.update(status="failed", provider=llm.name, notes=[str(exc)])
        return {"entries": (macro, corp), "narrative": narrative}

    def verify(self, s):
        macro, corp = s["entries"]
        events = {e.event_id: e for _, m in s["macro"] + s["corporate"] for e in m}
        checks = verify_entries(macro + corp, events, s["as_of"], s["window_start"])
        nar = s["narrative"]
        details: list[str] = []
        kept = []
        for it in nar.get("interpretations", []):
            ids = [i for i in it.get("event_ids", []) if i in events]
            universe = [x for i in ids for x in event_universe(events[i])]
            bad = check_numeric_claim(it.get("text", ""), universe)
            if not ids or bad or len(ids) != len(it.get("event_ids", [])):
                details.append(
                    f"Dropped model interpretation (unsupported numbers {bad} or unknown event ids)."
                )
            else:
                kept.append({"text": it["text"], "event_ids": ids, "kind": "model_interpretation"})
        if nar.get("summary") and check_numeric_claim(nar["summary"], []):
            details.append("Dropped model executive summary: it contained figures.")
            nar["summary"] = None
        nar["interpretations"] = kept
        checks.append(
            CheckResult(
                name="llm_output_screening",
                status="warning" if details else "passed",
                details=details or [f"LLM status: {nar['status']}"],
            )
        )
        missing = [
            f"{u.title} scheduled {u.scheduled_for.isoformat() if u.scheduled_for else '?'}: publication not "
            f"confirmed in available source data as of cutoff"
            for u in s["unconfirmed"]
        ]
        checks.append(
            CheckResult(
                name="scheduled_vs_published",
                status="warning" if missing else "passed",
                details=missing or ["Every scheduled release in the window has a confirmed publication."],
            )
        )
        return {"verification": summarize(checks), "narrative": nar}

    def persist(self, s):
        macro, corp = s["entries"]
        nar = s["narrative"]
        all_events = [e for _, m in s["macro"] + s["corporate"] for e in m]
        origins = sorted({e.data_origin for e in all_events})
        counts = summarize_counts(macro, corp)
        top = macro + corp
        top.sort(key=lambda e: -(e.importance_score or -1))
        if top:
            names = "; ".join(f"{e.title} [{e.importance_level}]" for e in top[:3])
            summary = (
                f"{counts['events']} verified events in {counts['macro_clusters']} macroeconomic and "
                f"{counts['corporate_clusters']} corporate clusters became available in the window. "
                f"Highest-ranked: {names}."
            )
        else:
            summary = "No source-verified events became available in this window."
        implications = sorted({w for e in macro + corp for w in e.why_it_matters})[:20]
        markets = sorted({a for e in macro + corp for a in e.affected})
        uncertainty = [
            "Market consensus forecasts are unavailable (no authenticated source); no surprise is reported.",
            "No market prices were used: no realized market reaction is claimed.",
            "Economic implications are hypotheses about direction, not estimated effects.",
        ]
        uncertainty += [
            f"{u.title}: scheduled for {u.scheduled_for.isoformat() if u.scheduled_for else 'unknown'}, "
            "publication not confirmed as of cutoff (not reported as occurred)."
            for u in s["unconfirmed"]
        ]
        if any(e.availability_precision == "DATE_ONLY_CONSERVATIVE" for e in all_events):
            uncertainty.append(
                "Some sources are date-stamped only; they are treated as available from the next day "
                "00:00 New York (conservative)."
            )
        if any(e.availability_precision == "SCHEDULED_TIME_CONFIRMED_DATE" for e in all_events):
            uncertainty.append(
                "Macro release times use the official scheduled time; the publication *date* is "
                "confirmed by ALFRED vintages, the intraday time is not independently verified."
            )
        if nar["status"] != "generated":
            uncertainty.append("LLM synthesis unavailable; narrative is template-based.")
        upcoming = [
            {
                "event_id": u.event_id,
                "title": u.title,
                "scheduled_for": u.scheduled_for.astimezone(ET).isoformat() if u.scheduled_for else None,
                "status": "SCHEDULED (not occurred)",
            }
            for u in s["upcoming"][:20]
        ]
        sources = [x for e in all_events for x in s["sources"].get(e.event_id, [])]
        # Counts quoted in the summary are evidence too.
        summary_bad = check_numeric_claim(
            summary,
            [(float(v), "count") for v in counts.values()]
            + [(e.importance_score or 0, "count") for e in top[:3]],
        )
        ver = s["verification"]
        if summary_bad:
            ver.checks.append(
                CheckResult(name="summary_numeric_claims", status="failed", details=summary_bad)
            )
            ver = summarize(ver.checks)
        report = BriefingReport(
            briefing_id=s["briefing_id"],
            status="completed",
            as_of=s["as_of"],
            window_start=s["window_start"],
            window_end=s["as_of"],
            generated_at=utcnow(),
            data_origins=origins,
            executive_summary=summary,
            model_executive_summary=nar.get("summary"),
            model_interpretations=nar.get("interpretations", []),
            macro_entries=macro,
            corporate_entries=corp,
            implications=implications,
            affected_markets=markets,
            upcoming=upcoming,
            uncertainty=uncertainty,
            sources=sources,
            verification=ver,
            llm={"status": nar["status"], "provider": nar["provider"], "notes": nar["notes"]},
            counts=counts,
        )
        report.markdown = render_markdown(report)
        rows = [(eid, e.section, e.rank, e.importance_score) for e in macro + corp for eid in e.event_ids]
        self.briefings.save_events(s["briefing_id"], rows)
        self.briefings.update(
            s["briefing_id"],
            status="completed",
            verification_status=ver.status,
            report_json=report.model_dump(mode="json"),
            markdown=report.markdown,
            completed_at=utcnow(),
            steps=s.get("steps", []),
        )
        return {"report": report}

    def fail(self, s):
        if s.get("as_of"):
            self.briefings.update(
                s["briefing_id"],
                status="failed",
                error=s.get("error"),
                completed_at=utcnow(),
                steps=s.get("steps", []),
            )
        return {}
