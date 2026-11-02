"""Economic transmission channels: evidence-linked hypotheses, not causal estimates.

Interpretation types are kept separate:
  ACCOUNTING_IDENTITY        true by definition (e.g. FCF = OCF − CAPEX)
  ECONOMIC_HYPOTHESIS        standard textbook mechanism; direction conditional on the
                             verified change; NOT estimated or tested here
  HISTORICAL_STATISTICAL     reserved for v0.3 event studies (not produced in v0.2)
  OBSERVED_MARKET_REACTION   reserved; requires verified market data (not produced)

No coefficients, probabilities or price impacts are produced.
"""

from __future__ import annotations

from finance_agent.events.schemas import EconomicChannel, EventSubtype, FinancialEvent

HYP = "ECONOMIC_HYPOTHESIS"
IDENTITY = "ACCOUNTING_IDENTITY"
BASIS = "Standard macro-finance reasoning (policy reaction function / discounting); direction only, not estimated."
COMMON_LIMITS = [
    "No market-consensus forecast available, so the 'news' content relative to expectations is unknown.",
    "No market reaction was observed or measured.",
]


def _dir(x: float | None) -> str:
    if x is None:
        return "not_determined"
    return "up" if x > 0 else "down" if x < 0 else "unchanged"


def _flip(d: str) -> str:
    return {"up": "down", "down": "up"}.get(d, d)


def _comp(ev: FinancialEvent, kind: str):
    return next((c for c in ev.comparisons if c.kind == kind and c.status == "ok"), None)


def channels_for(ev: FinancialEvent) -> list[EconomicChannel]:
    if not ev.has_occurred:
        return []
    out: list[EconomicChannel] = []

    def ch(src, tgt, direction, mech, evidence, itype=HYP, basis=BASIS, limits=None):
        out.append(
            EconomicChannel(
                trigger_event_id=ev.event_id,
                source_variable=src,
                target_variable=tgt,
                direction=direction,
                mechanism=mech,
                supporting_evidence=evidence,
                interpretation_type=itype,
                confidence_basis=basis,
                limitations=limits if limits is not None else list(COMMON_LIMITS),
            )
        )

    sub = ev.event_subtype
    if sub in {
        EventSubtype.CPI,
        EventSubtype.CORE_CPI,
        EventSubtype.PCE_PRICE_INDEX,
        EventSubtype.CORE_PCE_PRICE_INDEX,
    }:
        # Acceleration = this period's m/m change vs the previous period's (same vintage).
        accel = None if ev.value is None or ev.previous_value is None else ev.value - ev.previous_value
        d = _dir(accel)
        ev_ids = [ev.event_id] + [
            c.comparison_id
            for c in ev.comparisons
            if c.kind in {"pct_change_from_previous", "yoy_pct_change"}
        ]
        ch(
            "inflation (m/m acceleration)",
            "policy-rate expectations",
            d,
            "Faster inflation raises the expected path of the policy rate; slower inflation lowers it.",
            ev_ids,
        )
        ch(
            "policy-rate expectations",
            "short-dated Treasury yields (e.g. 2-year)",
            d,
            "Short-term yields embed expected policy rates.",
            ev_ids,
        )
        ch(
            "Treasury yields / discount rates",
            "duration-sensitive growth equities",
            _flip(d),
            "Higher discount rates reduce the present value of distant cash flows.",
            ev_ids,
        )
    elif sub == EventSubtype.NONFARM_PAYROLLS:
        accel = None if ev.value is None or ev.previous_value is None else ev.value - ev.previous_value
        d = _dir(accel)
        ch(
            "payroll growth (vs previous month)",
            "growth expectations / cyclical equities",
            d,
            "Stronger hiring signals stronger demand and earnings for cyclical sectors.",
            [ev.event_id],
        )
        ch(
            "payroll growth",
            "policy-rate expectations and Treasury yields",
            d,
            "A tighter labour market can raise expected policy rates.",
            [ev.event_id],
        )
    elif sub == EventSubtype.UNEMPLOYMENT_RATE:
        c = _comp(ev, "change_from_previous")
        d = _dir(c.value if c else None)
        ch(
            "unemployment rate",
            "growth expectations / cyclical equities",
            _flip(d),
            "Rising unemployment signals weaker demand.",
            [ev.event_id] + ([c.comparison_id] if c else []),
        )
        ch(
            "unemployment rate",
            "policy-rate expectations",
            _flip(d),
            "Labour-market slack lowers expected policy rates.",
            [ev.event_id],
        )
    elif sub == EventSubtype.REAL_GDP:
        accel = None if ev.value is None or ev.previous_value is None else ev.value - ev.previous_value
        ch(
            "real GDP growth (vs previous quarter)",
            "earnings expectations / cyclical industries",
            _dir(accel),
            "Faster output growth supports revenue and earnings expectations.",
            [ev.event_id],
        )
    elif sub in {EventSubtype.RATE_HIKE, EventSubtype.RATE_CUT, EventSubtype.RATE_HOLD}:
        c = _comp(ev, "rate_change")
        d = _dir(c.value if c else None)
        evidence = [ev.event_id] + ([c.comparison_id] if c else [])
        ch(
            "federal funds target range",
            "discount rates / short-term yields",
            d,
            "The policy rate anchors short-term funding and discount rates.",
            evidence,
        )
        ch(
            "discount rates",
            "duration-sensitive assets (long bonds, growth equities)",
            _flip(d),
            "Higher discount rates lower the value of long-duration cash flows.",
            evidence,
        )
        ch(
            "policy rate",
            "bank net interest margins",
            "ambiguous",
            "Depends on deposit betas and the yield-curve shape; sign not determined.",
            evidence,
        )
    elif sub == EventSubtype.FINANCIAL_METRIC_CHANGE:
        rev = _comp(ev, "metric_change:revenue_yoy")
        capex = _comp(ev, "metric:capex_yoy")
        ch(
            "free cash flow",
            "operating cash flow minus CAPEX",
            "not_determined",
            "FCF = operating cash flow − CAPEX (definition used in all calculations).",
            [ev.event_id],
            IDENTITY,
            "Accounting identity.",
            [],
        )
        ch(
            "revenue growth (change vs prior quarter)",
            "expected profitability / valuation",
            _dir(rev.value if rev else None),
            "Faster revenue growth tends to raise expected profits.",
            [ev.event_id] + ([rev.comparison_id] if rev else []),
            limits=[
                "Valuation impact depends on expectations not observed here.",
                "No market reaction measured.",
            ],
        )
        if capex is not None:
            ch(
                "CAPEX growth",
                "suppliers' revenue and the firm's near-term free cash flow",
                _dir(capex.value),
                "Capital spending is revenue for equipment/infrastructure suppliers and reduces near-term FCF.",
                [ev.event_id, capex.comparison_id],
                limits=[
                    "Supplier exposure not mapped to specific companies in v0.2.",
                    "No market reaction measured.",
                ],
            )
    elif sub == EventSubtype.EARNINGS_RELEASE:
        ch(
            "reported quarterly results",
            "expected profitability / valuation",
            "not_determined",
            "An earnings release updates information about profitability; direction requires the reported figures.",
            [ev.event_id],
            limits=[
                "Press-release text is not parsed in v0.2; see the linked metric-change event.",
                "No market reaction measured.",
            ],
        )
    return out
