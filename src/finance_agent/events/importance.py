"""Explainable, deterministic event-importance scoring.

importance_score = 100 * Σ component values / D,   D = 100, or 120 when a surprise exists

Components (default maxima): category 40, magnitude 25, extremeness 10, novelty 10,
relevance 15 (sum 100) and surprise 20. Surprise is the only component removed from the
denominator when it cannot be evaluated (no authenticated consensus), so missing
consensus never lowers a score. Every other component contributes 0 when not applicable
(e.g. a qualitative 8-K has no measurable magnitude), so qualitative disclosures cannot
outrank large verified quantitative changes merely through novelty and relevance.
A quantitative event whose magnitude cannot be measured gets level INSUFFICIENT_DATA and
no ranking score (its partial score is kept in the components for transparency).

The weights are judgmental defaults chosen for interpretability. They are NOT
empirically optimized or trained; ``ImportanceConfig`` can override them.
Source repetition never adds score: one event may have many sources, but scoring reads
only the event's verified values; cluster duplicates are handled by the briefing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from finance_agent.events.schemas import (
    EventStatus,
    EventSubtype,
    FinancialEvent,
    ImportanceLevel,
    RevisionStatus,
    VerificationStatus,
)

METHOD_VERSION = "importance-v0.2.1"
# Constants quoted in explanation text (registered as evidence for numeric verification).
RULE_CONSTANTS = (0.1, 1, 2, 10, 25, 50)

DEFAULT_CATEGORY_WEIGHTS: dict[str, float] = {
    EventSubtype.RATE_HIKE: 40,
    EventSubtype.RATE_CUT: 40,
    EventSubtype.RATE_HOLD: 30,
    EventSubtype.POLICY_STATEMENT: 25,
    EventSubtype.CPI: 34,
    EventSubtype.CORE_CPI: 32,
    EventSubtype.NONFARM_PAYROLLS: 34,
    EventSubtype.UNEMPLOYMENT_RATE: 30,
    EventSubtype.CORE_PCE_PRICE_INDEX: 30,
    EventSubtype.PCE_PRICE_INDEX: 26,
    EventSubtype.REAL_GDP: 30,
    EventSubtype.EARNINGS_RELEASE: 26,
    EventSubtype.FINANCIAL_METRIC_CHANGE: 22,
    EventSubtype.FORM_10K: 16,
    EventSubtype.FORM_10Q: 14,
    EventSubtype.ACQUISITION_DISPOSITION: 22,
    EventSubtype.CYBERSECURITY_INCIDENT: 22,
    EventSubtype.LEADERSHIP_CHANGE: 16,
    EventSubtype.MATERIAL_AGREEMENT: 14,
    EventSubtype.FINANCING: 12,
    EventSubtype.OTHER_MATERIAL_EVENT: 10,
    EventSubtype.GOVERNANCE_CHANGE: 6,
    EventSubtype.SHAREHOLDER_VOTE: 6,
    EventSubtype.REG_FD_DISCLOSURE: 6,
    EventSubtype.FORM_8K: 6,
    EventSubtype.AMENDMENT: 6,
}
QUANTITATIVE = {
    EventSubtype.CPI,
    EventSubtype.CORE_CPI,
    EventSubtype.NONFARM_PAYROLLS,
    EventSubtype.UNEMPLOYMENT_RATE,
    EventSubtype.PCE_PRICE_INDEX,
    EventSubtype.CORE_PCE_PRICE_INDEX,
    EventSubtype.REAL_GDP,
    EventSubtype.RATE_HIKE,
    EventSubtype.RATE_CUT,
    EventSubtype.RATE_HOLD,
    EventSubtype.FINANCIAL_METRIC_CHANGE,
}


@dataclass(frozen=True)
class ImportanceConfig:
    category_weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_CATEGORY_WEIGHTS))
    max_category: float = 40
    max_magnitude: float = 25
    max_surprise: float = 20
    max_extremeness: float = 10
    max_novelty: float = 10
    max_relevance: float = 15
    high_threshold: float = 60
    medium_threshold: float = 40
    revision_weight_factor: float = 0.5
    tracked_tickers: frozenset[str] = frozenset({"MSFT", "AAPL", "AMZN", "GOOGL", "META"})


def _comp(ev: FinancialEvent, kind: str) -> float | None:
    c = next((c for c in ev.comparisons if c.kind == kind and c.status == "ok"), None)
    return c.value if c else None


def score_event(
    ev: FinancialEvent, cfg: ImportanceConfig | None = None
) -> tuple[float | None, ImportanceLevel, dict]:
    """Return (score 0-100 or None, level, components with explanations)."""
    cfg = cfg or ImportanceConfig()
    comps: dict[str, dict] = {}

    def put(name: str, value: float | None, maximum: float, why: str) -> None:
        comps[name] = {
            "value": None if value is None else round(min(max(value, 0.0), maximum), 4),
            "max": maximum,
            "available": value is not None,
            "explanation": why,
        }

    if not ev.has_occurred or ev.verification_status != VerificationStatus.SOURCE_VERIFIED:
        return (
            None,
            ImportanceLevel.INSUFFICIENT_DATA,
            {
                "method": METHOD_VERSION,
                "note": "Only source-verified published/revised events are scored; schedules are not.",
            },
        )

    base_weight = cfg.category_weights.get(ev.event_subtype, 8)
    if ev.revision_status == RevisionStatus.REVISION:
        put(
            "category",
            base_weight * cfg.revision_weight_factor,
            cfg.max_category,
            f"Half of the base weight for {ev.event_subtype}: revision of previously published data.",
        )
    else:
        put(
            "category",
            base_weight,
            cfg.max_category,
            f"Base weight for {ev.event_subtype} (judgmental default).",
        )

    # ---- magnitude of the verified change
    z = _comp(ev, "standardized_change")
    bp = _comp(ev, "rate_change")
    if ev.event_subtype in {EventSubtype.RATE_HIKE, EventSubtype.RATE_CUT, EventSubtype.RATE_HOLD}:
        if bp is not None:
            put(
                "magnitude",
                abs(bp) / 25 * 10,
                cfg.max_magnitude,
                f"|{bp:+.0f} bp| target-range change; 25 bp = 10 points.",
            )
        else:
            put("magnitude", None, cfg.max_magnitude, "No previous verified decision to compare.")
    elif ev.revision_status == RevisionStatus.REVISION:
        sd = ev.attributes.get("headline_sd")
        if ev.value is not None and ev.previous_value is not None and sd:
            put(
                "magnitude",
                10 * abs(ev.value - ev.previous_value) / sd,
                cfg.max_magnitude,
                "Headline revision measured in historical standard deviations of the headline; 1 sd = 10 points.",
            )
        else:
            put(
                "magnitude",
                None,
                cfg.max_magnitude,
                "Revision size could not be measured (no headline history).",
            )
    elif ev.event_subtype == EventSubtype.FINANCIAL_METRIC_CHANGE:
        d = _comp(ev, "metric_change:revenue_yoy")
        if d is not None:
            put(
                "magnitude",
                abs(d) * 2,
                cfg.max_magnitude,
                f"Revenue YoY growth changed {d:+.1f} pp vs prior quarter; 1 pp = 2 points.",
            )
        else:
            put("magnitude", None, cfg.max_magnitude, "No prior-quarter comparison available.")
    elif ev.event_subtype in QUANTITATIVE:
        if z is not None:
            put(
                "magnitude",
                abs(z) * 10,
                cfg.max_magnitude,
                f"Headline is {z:+.2f} standard deviations from its prior history; 1 sd = 10 points.",
            )
        else:
            put("magnitude", None, cfg.max_magnitude, "Insufficient history for a standardized change.")
    # non-quantitative disclosures: magnitude not applicable (omitted from the denominator)

    # ---- surprise: only with authenticated consensus
    s = _comp(ev, "standardized_surprise")
    if ev.consensus_status == "available" and s is not None:
        put("surprise", abs(s) * 10, cfg.max_surprise, f"Standardized surprise {s:+.2f}.")
    else:
        comps["surprise"] = {
            "value": None,
            "max": cfg.max_surprise,
            "available": False,
            "explanation": "consensus_status=unavailable: no authenticated consensus; excluded, not zero.",
        }

    # ---- historical extremeness
    pct = _comp(ev, "percentile")
    if pct is not None:
        put(
            "extremeness",
            (abs(pct - 50) / 50) ** 2 * cfg.max_extremeness,
            cfg.max_extremeness,
            f"Headline at the {pct:.0f}th percentile of prior history.",
        )

    # ---- novelty
    if ev.revision_status == RevisionStatus.REVISION:
        put("novelty", 4, cfg.max_novelty, "Revision of previously published data.")
    elif ev.event_subtype == EventSubtype.AMENDMENT:
        put("novelty", 3, cfg.max_novelty, "Amendment to a prior filing.")
    else:
        put("novelty", 10, cfg.max_novelty, "First publication of this information.")

    # ---- relevance to tracked markets / companies
    if set(ev.affected_entities) & cfg.tracked_tickers:
        put("relevance", 15, cfg.max_relevance, "Concerns a tracked company.")
    elif ev.event_type in {"MACRO_RELEASE", "CENTRAL_BANK"}:
        put(
            "relevance", 12, cfg.max_relevance, "Economy-wide indicator relevant to rates and equity markets."
        )
    else:
        put("relevance", 5, cfg.max_relevance, "Not linked to a tracked market or company.")

    for name, mx, why in (
        ("magnitude", cfg.max_magnitude, "Not applicable: qualitative disclosure (contributes 0)."),
        ("extremeness", cfg.max_extremeness, "No historical distribution available (contributes 0)."),
    ):
        if name not in comps:
            comps[name] = {"value": None, "max": mx, "available": False, "explanation": why}
    numerator = sum(c["value"] for k, c in comps.items() if c["available"])
    denominator = sum(c["max"] for k, c in comps.items() if k != "surprise" or c["available"])
    score = round(100 * numerator / denominator, 2)
    magnitude_missing = ev.event_subtype in QUANTITATIVE and not comps["magnitude"]["available"]
    if magnitude_missing:
        level = ImportanceLevel.INSUFFICIENT_DATA
        comps["partial_score"] = score
        score = None  # not ranked on partial information
    elif score >= cfg.high_threshold:
        level = ImportanceLevel.HIGH
    elif score >= cfg.medium_threshold:
        level = ImportanceLevel.MEDIUM
    else:
        level = ImportanceLevel.LOW
    comps["method"] = METHOD_VERSION
    comps["formula"] = (
        "100 * sum(component values) / (sum of maxima, excluding surprise when no consensus); "
        "unavailable components contribute 0"
    )
    return score, level, comps


def apply_importance(ev: FinancialEvent, cfg: ImportanceConfig | None = None) -> FinancialEvent:
    score, level, comps = score_event(ev, cfg)
    ev.importance_score, ev.importance_level, ev.importance_components = score, level, comps
    return ev


def is_ranked(ev: FinancialEvent) -> bool:
    return ev.event_status in {EventStatus.PUBLISHED, EventStatus.REVISED} and ev.importance_score is not None
