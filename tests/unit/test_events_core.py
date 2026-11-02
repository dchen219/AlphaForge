"""v0.2 core: event schema invariants, comparisons, surprise gating, importance, channels.

All inputs here are SYNTHETIC values constructed in the test (not real observations).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from finance_agent.events.comparisons import (
    MIN_HISTORY,
    ConsensusObservation,
    IndicatorSpec,
    compute_comparisons,
    headline_series,
    revision_comparison,
    shift_months,
    surprise,
)
from finance_agent.events.importance import ImportanceConfig, score_event
from finance_agent.events.schemas import (
    AvailabilityPrecision,
    Comparison,
    ConsensusStatus,
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    ImportanceLevel,
    RevisionStatus,
    Unit,
    VerificationStatus,
    make_event_id,
)
from finance_agent.events.transmission import channels_for

T0 = datetime(2026, 10, 14, 12, 30, tzinfo=UTC)
CPI_SPEC = IndicatorSpec("CPIAUCSL", "pct_change", Unit.PERCENT_CHANGE, Unit.INDEX_POINTS, "M", "pct")
UNRATE_SPEC = IndicatorSpec("UNRATE", "level", Unit.PERCENT, Unit.PERCENT, "M", "diff")


def macro_event(**kw) -> FinancialEvent:
    base = dict(
        canonical_event_key="MACRO|CPIAUCSL|2026-09|FIRST_RELEASE",
        event_type=EventCategory.MACRO_RELEASE,
        event_subtype=EventSubtype.CPI,
        event_status=EventStatus.PUBLISHED,
        title="CPI — September 2026",
        source_name="BLS via ALFRED",
        available_at=T0,
        availability_precision=AvailabilityPrecision.EXACT_TIMESTAMP,
        value=0.4,
        previous_value=0.2,
        unit=Unit.PERCENT_CHANGE,
        revision_status=RevisionStatus.FIRST_RELEASE,
        verification_status=VerificationStatus.SOURCE_VERIFIED,
        affected_entities=["CPIAUCSL"],
    )
    base.update(kw)
    return FinancialEvent(**base)


# ---------------------------------------------------------------- schema


def test_event_id_is_deterministic_from_canonical_key():
    a, b = macro_event(), macro_event(title="different title")
    assert a.event_id == b.event_id == make_event_id("MACRO|CPIAUCSL|2026-09|FIRST_RELEASE")


def test_subtype_must_match_category():
    with pytest.raises(ValidationError, match="not valid for category"):
        macro_event(event_type=EventCategory.CENTRAL_BANK)


def test_timestamps_must_be_timezone_aware():
    with pytest.raises(ValidationError, match="timezone-aware"):
        macro_event(available_at=datetime(2026, 10, 14, 8, 30))


def test_scheduled_event_cannot_carry_observed_values():
    with pytest.raises(ValidationError, match="SCHEDULED"):
        macro_event(event_status=EventStatus.SCHEDULED, event_subtype=EventSubtype.RELEASE_SCHEDULED)
    sched = macro_event(
        event_status=EventStatus.SCHEDULED,
        event_subtype=EventSubtype.RELEASE_SCHEDULED,
        value=None,
        previous_value=None,
        available_at=None,
    )
    assert not sched.has_occurred


def test_consensus_value_requires_available_status():
    with pytest.raises(ValidationError, match="consensus"):
        macro_event(consensus_value=0.3)
    assert macro_event().consensus_status == ConsensusStatus.UNAVAILABLE


# ---------------------------------------------------------------- comparisons (hand-checked)


def levels(n: int = 40, start=date(2023, 1, 1), base=300.0, step=0.6) -> dict[date, float]:
    return {shift_months(start, i): base + step * i for i in range(n)}


def test_shift_months_and_headline_series():
    assert shift_months(date(2026, 1, 1), -1) == date(2025, 12, 1)
    assert shift_months(date(2026, 1, 1), 14) == date(2027, 3, 1)
    hs = headline_series({date(2026, 1, 1): 100.0, date(2026, 2, 1): 101.0}, "pct_change", "M")
    assert hs == {date(2026, 2, 1): pytest.approx(1.0)}
    q = headline_series({date(2026, 1, 1): 100.0, date(2026, 4, 1): 101.0}, "annualized_pct_change", "Q")
    assert q[date(2026, 4, 1)] == pytest.approx((1.01**4 - 1) * 100)


def test_cpi_comparisons_hand_calculated():
    lv = {date(2025, 9, 1): 300.0, date(2026, 8, 1): 309.0, date(2026, 9, 1): 310.236}
    comps = {c.kind: c for c in compute_comparisons(CPI_SPEC, lv, date(2026, 9, 1), "2026-10-14")}
    assert comps["change_from_previous"].value == pytest.approx(1.236)
    assert comps["change_from_previous"].unit == Unit.INDEX_POINTS
    assert comps["pct_change_from_previous"].value == pytest.approx(0.4)  # 310.236/309 - 1
    assert comps["yoy_pct_change"].value == pytest.approx(3.412)  # 310.236/300 - 1
    assert comps["percentile"].status == "unavailable"  # too little history
    assert all(c.inputs and c.formula for c in comps.values() if c.status == "ok")


def test_rate_series_uses_percentage_points():
    lv = {date(2025, 9, 1): 4.4, date(2026, 8, 1): 4.1, date(2026, 9, 1): 4.2}
    comps = {c.kind: c for c in compute_comparisons(UNRATE_SPEC, lv, date(2026, 9, 1), "v")}
    assert comps["change_from_previous"].unit == Unit.PERCENTAGE_POINTS
    assert comps["change_from_previous"].value == pytest.approx(0.1)
    assert comps["yoy_change"].value == pytest.approx(-0.2) and "pct_change_from_previous" not in comps


def test_percentile_and_standardized_need_minimum_history():
    lv = levels(MIN_HISTORY + 3)
    last = max(lv)
    lv[last] = lv[shift_months(last, -1)] * 1.01  # an unusually large monthly increase
    comps = {c.kind: c for c in compute_comparisons(CPI_SPEC, lv, last, "v")}
    assert comps["percentile"].value == pytest.approx(100.0)
    assert comps["standardized_change"].value > 3
    few = levels(10)
    comps2 = {c.kind: c for c in compute_comparisons(CPI_SPEC, few, max(few), "v")}
    assert "standardized_change" not in comps2 and comps2["percentile"].status == "unavailable"


def test_revision_comparison():
    c = revision_comparison(
        "PAYEMS",
        date(2026, 8, 1),
        159_000.0,
        158_940.0,
        "2026-10-01",
        "2026-10-02",
        Unit.THOUSANDS_OF_PERSONS,
    )
    assert c.value == pytest.approx(-60.0) and c.kind == "revision" and len(c.inputs) == 2


# ---------------------------------------------------------------- surprise gating


def test_no_consensus_means_no_surprise_and_previous_value_is_never_used():
    status, comps = surprise(0.4, Unit.PERCENT_CHANGE, T0, None)
    assert status == ConsensusStatus.UNAVAILABLE and comps == []


def test_consensus_published_after_release_is_rejected():
    late = ConsensusObservation(
        0.3, Unit.PERCENT_CHANGE, "SYNTHETIC survey", "https://example.test", T0 + timedelta(minutes=1)
    )
    assert surprise(0.4, Unit.PERCENT_CHANGE, T0, late)[0] == ConsensusStatus.UNAVAILABLE


def test_consensus_unit_mismatch_is_rejected():
    c = ConsensusObservation(0.3, Unit.PERCENT, "SYNTHETIC", "https://example.test", T0 - timedelta(days=1))
    assert surprise(0.4, Unit.PERCENT_CHANGE, T0, c)[0] == ConsensusStatus.UNAVAILABLE


def test_valid_consensus_surprise_and_standardization():
    c = ConsensusObservation(
        0.3, Unit.PERCENT_CHANGE, "SYNTHETIC survey", "https://example.test", T0 - timedelta(days=1)
    )
    status, comps = surprise(0.4, Unit.PERCENT_CHANGE, T0, c)
    assert status == ConsensusStatus.AVAILABLE and comps[0].value == pytest.approx(0.1) and len(comps) == 1
    hist = [0.1, -0.1] * 10  # 20 synthetic historical surprises, sd = 0.1026
    status, comps = surprise(0.4, Unit.PERCENT_CHANGE, T0, c, hist)
    assert comps[1].kind == "standardized_surprise" and comps[1].value == pytest.approx(
        0.1 / 0.10259783520851541
    )
    assert len(surprise(0.4, Unit.PERCENT_CHANGE, T0, c, hist[:19])[1]) == 1  # < 20: not standardized


# ---------------------------------------------------------------- importance


def with_comps(ev: FinancialEvent, **kinds: float) -> FinancialEvent:
    ev.comparisons = [
        Comparison(comparison_id=f"CMP-{k}", kind=k, label=k, value=v, unit=Unit.COUNT, formula="f")
        for k, v in kinds.items()
    ]
    return ev


def test_missing_consensus_is_excluded_not_zero():
    ev = with_comps(macro_event(), standardized_change=2.0, percentile=95.0)
    score, level, comps = score_event(ev)
    assert (
        comps["surprise"]["available"] is False and "excluded, not zero" in comps["surprise"]["explanation"]
    )
    # Fixed denominator 100 (surprise excluded only because consensus is unavailable).
    expected = 100 * (34 + 20 + (45 / 50) ** 2 * 10 + 10 + 12) / (40 + 25 + 10 + 10 + 15)
    assert score == pytest.approx(round(expected, 2)) and level == ImportanceLevel.HIGH


def test_qualitative_disclosure_cannot_ride_on_novelty_and_relevance():
    """Regression: a 10-K must not outrank a verified FOMC rate change (v0.2.0 calibration bug)."""
    tenk = FinancialEvent(
        canonical_event_key="SEC|1|x",
        event_type=EventCategory.CORPORATE_FILING,
        event_subtype=EventSubtype.FORM_10K,
        event_status=EventStatus.PUBLISHED,
        title="10-K",
        source_name="SEC EDGAR",
        available_at=T0,
        affected_entities=["MSFT"],
        verification_status=VerificationStatus.SOURCE_VERIFIED,
    )
    hold = FinancialEvent(
        canonical_event_key="FOMC|DECISION|x",
        event_type=EventCategory.CENTRAL_BANK,
        event_subtype=EventSubtype.RATE_HOLD,
        event_status=EventStatus.PUBLISHED,
        title="hold",
        source_name="Federal Reserve",
        available_at=T0,
        value=4.0,
        previous_value=4.0,
        unit=Unit.PERCENT,
        verification_status=VerificationStatus.SOURCE_VERIFIED,
    )
    with_comps(hold, rate_change=0.0)
    s10k, lvl10k, c10k = score_event(tenk)
    assert c10k["magnitude"]["value"] is None and "contributes 0" in c10k["magnitude"]["explanation"]
    assert s10k == pytest.approx(41.0) and lvl10k == ImportanceLevel.MEDIUM  # (16 + 10 + 15) / 100
    assert score_event(hold)[0] > s10k


def test_revision_scored_in_standard_deviations_at_half_weight():
    rev = macro_event(
        canonical_event_key="MACRO|PAYEMS|2026-08|REVISION|2026-10-02",
        event_subtype=EventSubtype.NONFARM_PAYROLLS,
        event_status=EventStatus.REVISED,
        revision_status=RevisionStatus.REVISION,
        value=133.0,
        previous_value=162.0,
        unit=Unit.THOUSANDS_OF_PERSONS,
        attributes={"headline_sd": 145.0},
    )
    score, _level, comps = score_event(rev)
    assert comps["category"]["value"] == 17  # half of 34
    assert comps["magnitude"]["value"] == pytest.approx(10 * 29 / 145)
    first = with_comps(macro_event(event_subtype=EventSubtype.NONFARM_PAYROLLS), standardized_change=0.5)
    assert score_event(first)[0] > score  # a first release outranks a modest revision
    rev.attributes = {}
    assert score_event(rev)[1] == ImportanceLevel.INSUFFICIENT_DATA


def test_insufficient_data_has_no_ranking_score_but_keeps_partial():
    score, level, comps = score_event(macro_event())
    assert score is None and level == ImportanceLevel.INSUFFICIENT_DATA and comps["partial_score"] > 0


def test_quantitative_event_without_magnitude_is_insufficient_data():
    _score, level, comps = score_event(macro_event())
    assert level == ImportanceLevel.INSUFFICIENT_DATA and comps["magnitude"]["available"] is False


def test_bigger_verified_change_ranks_higher_and_scoring_is_deterministic():
    small = with_comps(macro_event(), standardized_change=0.2, percentile=55.0)
    big = with_comps(macro_event(), standardized_change=2.5, percentile=99.0)
    assert score_event(big)[0] > score_event(small)[0]
    assert score_event(big) == score_event(big)


def test_schedules_are_never_scored():
    sched = macro_event(
        event_status=EventStatus.SCHEDULED,
        event_subtype=EventSubtype.RELEASE_SCHEDULED,
        value=None,
        previous_value=None,
        verification_status=VerificationStatus.SCHEDULE_ONLY,
    )
    assert score_event(sched)[:2] == (None, ImportanceLevel.INSUFFICIENT_DATA)


def test_rate_decision_magnitude_from_bp_change():
    hike = FinancialEvent(
        canonical_event_key="FOMC|DECISION|2026-09-16",
        event_type=EventCategory.CENTRAL_BANK,
        event_subtype=EventSubtype.RATE_HIKE,
        event_status=EventStatus.PUBLISHED,
        title="FOMC raised",
        source_name="Federal Reserve",
        available_at=T0,
        value=4.0,
        previous_value=3.75,
        unit=Unit.PERCENT,
        verification_status=VerificationStatus.SOURCE_VERIFIED,
    )
    with_comps(hike, rate_change=25.0)
    _, level, comps = score_event(hike)
    assert comps["magnitude"]["value"] == 10 and level == ImportanceLevel.HIGH


def test_config_weights_are_overridable():
    ev = with_comps(macro_event(), standardized_change=1.0)
    cfg = ImportanceConfig(category_weights={EventSubtype.CPI: 5})
    assert score_event(ev, cfg)[0] < score_event(ev)[0]


# ---------------------------------------------------------------- transmission channels


def test_inflation_channels_direction_and_labels():
    chans = channels_for(macro_event(value=0.5, previous_value=0.2))
    assert {c.interpretation_type for c in chans} == {"ECONOMIC_HYPOTHESIS"}
    assert chans[0].direction == "up" and chans[2].direction == "down"  # yields up -> growth equities down
    assert all(c.limitations and c.supporting_evidence for c in chans)
    assert channels_for(macro_event(value=0.1, previous_value=0.4))[0].direction == "down"


def test_no_channels_for_scheduled_events_and_no_observed_reactions():
    sched = macro_event(
        event_status=EventStatus.SCHEDULED,
        event_subtype=EventSubtype.RELEASE_SCHEDULED,
        value=None,
        previous_value=None,
    )
    assert channels_for(sched) == []
    types = {c.interpretation_type for c in channels_for(macro_event())}
    assert "OBSERVED_MARKET_REACTION" not in types and "HISTORICAL_STATISTICAL" not in types
