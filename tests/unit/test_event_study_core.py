"""v0.3-B event-study core: alignment, windows, estimators and inference.

Statistical checks use INDEPENDENT references: hand-derived closed forms, explicit covariance
matrices (numpy.linalg.lstsq / full matrix algebra instead of the implementation's statsmodels +
g-vector formula), scipy.stats.ttest_1samp, and a seeded Monte Carlo size/coverage experiment.
All data here is SYNTHETIC and constructed inline.
"""

from __future__ import annotations

import math
from datetime import UTC, date, datetime

import numpy as np
import pytest
from scipy import stats

from finance_agent.event_studies import aggregation as agg
from finance_agent.event_studies.alignment import align_event, align_timestamp, event_time_for
from finance_agent.event_studies.engine import origin_of, window_timing_ok
from finance_agent.event_studies.inference import (
    car_variance_market_model,
    market_adjusted_inference,
    market_model_inference,
    placebo_distribution,
)
from finance_agent.event_studies.market_model import UnstableEstimation, fit_market_adjusted, fit_market_model
from finance_agent.event_studies.schemas import (
    DataMode,
    EstimationWindow,
    EventStudySpecification,
    EventWindow,
    InferenceStatus,
    TimeBasis,
    TimingClass,
    parse_model,
)
from finance_agent.event_studies.windows import session_grid
from finance_agent.event_studies.yields_study import yield_day0
from finance_agent.events.schemas import (
    AvailabilityPrecision,
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
)
from finance_agent.market.calendar import get_calendar
from finance_agent.timeutil import ET

CAL = get_calendar()
VB = TimeBasis.VERIFIED_TIMESTAMP


def ny(y, m, d, hh=0, mm=0, ss=0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=ET)


def align(t: datetime, basis=VB):
    return align_timestamp("EV-test", t, CAL, basis=basis)


# ---------------------------------------------------------------------------- alignment
def test_cpi_0830_is_pre_market_same_session():
    a = align(ny(2026, 10, 14, 8, 30), TimeBasis.OFFICIAL_SCHEDULE_CONFIRMED_DATE)
    assert a.timing_class == TimingClass.PRE_MARKET and a.day0 == date(2026, 10, 14)
    assert a.contamination_warning is None and "scheduled" in a.notes[0]


def test_fomc_1400_is_regular_session_with_contamination_flag():
    a = align(ny(2025, 9, 17, 14, 0))
    assert a.timing_class == TimingClass.REGULAR_SESSION and a.day0 == date(2025, 9, 17)
    assert "BEFORE the release" in a.contamination_warning


def test_earnings_exactly_at_1600_is_at_close_next_session():
    a = align(ny(2026, 10, 28, 16, 0, 0))
    assert a.timing_class == TimingClass.AT_CLOSE and a.day0 == date(2026, 10, 29)
    b = align(ny(2026, 10, 28, 15, 59, 59))
    assert b.timing_class == TimingClass.REGULAR_SESSION and b.day0 == date(2026, 10, 28)


def test_after_hours_earnings_attributed_to_next_session_over_weekend():
    a = align(ny(2026, 10, 30, 16, 5, 27))  # Friday after the close
    assert a.timing_class == TimingClass.AFTER_HOURS and a.day0 == date(2026, 11, 2)


def test_early_close_day_after_thanksgiving():
    # 2025-11-28 closes at 13:00 ET: 13:00 is AT the close, 12:30 is regular, 14:00 is after hours.
    assert align(ny(2025, 11, 28, 13, 0)).timing_class == TimingClass.AT_CLOSE
    assert align(ny(2025, 11, 28, 13, 0)).day0 == date(2025, 12, 1)
    assert align(ny(2025, 11, 28, 13, 0)).early_close
    assert align(ny(2025, 11, 28, 12, 30)).timing_class == TimingClass.REGULAR_SESSION
    late = align(ny(2025, 11, 28, 14, 0))
    assert late.timing_class == TimingClass.AFTER_HOURS and late.day0 == date(2025, 12, 1)
    assert any("early-close" in n for n in late.notes)


def test_holiday_and_weekend_map_to_next_session():
    a = align(ny(2025, 7, 4, 10, 0))  # Independence Day
    assert a.timing_class == TimingClass.NON_TRADING_DAY and a.day0 == date(2025, 7, 7)
    assert align(ny(2026, 10, 31, 9, 0)).day0 == date(2026, 11, 2)  # Saturday


def test_dst_transitions_use_real_utc_session_boundaries():
    # 2026-03-09 (first session after DST starts): open 13:30Z. 13:20Z is pre-market, 13:30Z is the open.
    assert align(datetime(2026, 3, 9, 13, 20, tzinfo=UTC)).timing_class == TimingClass.PRE_MARKET
    assert align(datetime(2026, 3, 9, 13, 30, tzinfo=UTC)).timing_class == TimingClass.REGULAR_SESSION
    # 2026-03-06 (before DST): 13:45Z = 08:45 EST is still pre-market.
    assert align(datetime(2026, 3, 6, 13, 45, tzinfo=UTC)).timing_class == TimingClass.PRE_MARKET
    # 2026-11-02 (after DST ends): close 21:00Z; 20:30Z is still regular trading.
    assert align(datetime(2026, 11, 2, 20, 30, tzinfo=UTC)).timing_class == TimingClass.REGULAR_SESSION
    assert align(datetime(2026, 10, 30, 20, 30, tzinfo=UTC)).timing_class == TimingClass.AFTER_HOURS


def test_uncertain_and_unknown_publication_times():
    a = align_timestamp("EV-x", None, CAL, basis=TimeBasis.DATE_ONLY, date_only=date(2026, 10, 29))
    assert (
        a.timing_class == TimingClass.UNCERTAIN_TIME and a.timing_uncertain and a.day0 == date(2026, 10, 29)
    )
    assert not window_timing_ok(EventWindow(start=0, end=0), a)[0]
    assert window_timing_ok(EventWindow(start=0, end=1), a)[0]
    assert window_timing_ok(EventWindow(start=-1, end=1), a)[0]
    assert not window_timing_ok(EventWindow(start=1, end=5), a)[0]
    sat = align_timestamp("EV-x", None, CAL, basis=TimeBasis.DATE_ONLY, date_only=date(2026, 10, 31))
    assert sat.day0 == date(2026, 11, 2) and not sat.timing_uncertain  # any time Saturday -> Monday
    none = align_timestamp("EV-x", None, CAL, basis=TimeBasis.NONE)
    assert none.timing_class == TimingClass.UNKNOWN and not none.eligible


def _event(**kw) -> FinancialEvent:
    base = dict(
        canonical_event_key="synthetic:test", event_type=EventCategory.MACRO_RELEASE,
        event_subtype=EventSubtype.REAL_GDP, event_status=EventStatus.PUBLISHED, title="SYNTHETIC",
        source_name="BEA via ALFRED", data_origin="fixture:synthetic",
    )  # fmt: skip
    base.update(kw)
    return FinancialEvent(**base)


def test_event_time_policy_never_invents_a_time():
    exact = _event(published_at=ny(2026, 10, 28, 16, 4, 44), available_at=ny(2026, 10, 28, 16, 4, 44),
                   availability_precision=AvailabilityPrecision.EXACT_TIMESTAMP)  # fmt: skip
    assert event_time_for(exact)[2] == TimeBasis.VERIFIED_TIMESTAMP
    sched = _event(available_at=ny(2026, 10, 14, 8, 30),
                   availability_precision=AvailabilityPrecision.SCHEDULED_TIME_CONFIRMED_DATE)  # fmt: skip
    assert event_time_for(sched) == (
        ny(2026, 10, 14, 8, 30),
        None,
        TimeBasis.OFFICIAL_SCHEDULE_CONFIRMED_DATE,
    )
    dateonly = _event(
        available_at=ny(2026, 10, 30), availability_precision=AvailabilityPrecision.DATE_ONLY_CONSERVATIVE
    )
    t, d, basis = event_time_for(dateonly)
    assert t is None and d == date(2026, 10, 29) and basis == TimeBasis.DATE_ONLY
    assert align_event(dateonly, CAL).timing_class == TimingClass.UNCERTAIN_TIME
    unknown = _event(available_at=ny(2026, 10, 30, 9), availability_precision=AvailabilityPrecision.UNKNOWN)
    assert not align_event(unknown, CAL).eligible


# ---------------------------------------------------------------------------- windows & specs
def test_session_grid_counts_trading_sessions_not_calendar_days():
    g = session_grid(CAL, date(2025, 7, 7), -2, 2)
    assert g[-1] == date(2025, 7, 3) and g[-2] == date(2025, 7, 2)  # 07-04 holiday, weekend skipped
    assert g[1] == date(2025, 7, 8)
    assert EventWindow.parse("-1:1") == EventWindow(start=-1, end=1)
    assert EventWindow.parse("[0,+5]").label == "[0,+5]"
    assert EventWindow(start=-1, end=1).label == "[-1,+1]" and EventWindow(start=0, end=0).label == "[0,0]"


def test_specification_validation_and_hash():
    s = EventStudySpecification(event_id="EV-1", target="QQQ", benchmark={"instrument_id": "SPY"})
    assert s.spec_hash() == EventStudySpecification(event_id="EV-1", target="QQQ",
                                                    benchmark={"instrument_id": "spy"}).spec_hash()  # fmt: skip
    assert s.spec_hash() != s.model_copy(update={"price_basis": "raw"}).spec_hash()
    with pytest.raises(ValueError, match="end before every event window"):
        EventStudySpecification(event_id="EV-1", target="QQQ", benchmark={"instrument_id": "SPY"},
                                estimation_window=EstimationWindow(start=-30, end=-1, min_observations=20),
                                event_windows=(EventWindow(start=-2, end=1),))  # fmt: skip
    with pytest.raises(ValueError, match="takes no as_of"):
        EventStudySpecification(event_id="EV-1", target="QQQ", benchmark={"instrument_id": "SPY"},
                                as_of=ny(2026, 1, 1))  # fmt: skip
    with pytest.raises(ValueError, match="require an explicit as_of"):
        EventStudySpecification(event_id="EV-1", target="QQQ", benchmark={"instrument_id": "SPY"},
                                data_mode=DataMode.POINT_IN_TIME)  # fmt: skip
    with pytest.raises(ValueError, match="require a benchmark"):
        EventStudySpecification(event_id="EV-1", target="QQQ")
    with pytest.raises(ValueError, match="price_basis"):
        EventStudySpecification(
            event_id="EV-1", target="QQQ", benchmark={"instrument_id": "SPY"}, price_basis="adj"
        )
    with pytest.raises(ValueError):
        EventWindow(start=0, end=900)
    assert parse_model("market-model") == parse_model("market_model")
    with pytest.raises(ValueError):
        parse_model("capm-ml")


# ---------------------------------------------------------------------------- estimators
# Hand-derived case: rm sums to 0, sum rm^2 = 0.001; residuals e are orthogonal to (1, rm),
# so OLS returns alpha = 0.001 and beta = 1.5 exactly, SSR = 1e-5, sigma^2 = 1e-5 / 3.
RM = np.array([-0.02, -0.01, 0.0, 0.01, 0.02])
E = np.array([0.001, -0.002, 0.0, 0.002, -0.001])
RI = 0.001 + 1.5 * RM + E


def test_hand_calculated_market_model_and_car_inference():
    fit = fit_market_model(RI, RM, min_obs=3)
    assert fit.alpha == pytest.approx(0.001, abs=1e-15) and fit.beta == pytest.approx(1.5, abs=1e-12)
    assert fit.sigma2 == pytest.approx(1e-5 / 3, rel=1e-12) and fit.df == 3
    np.testing.assert_allclose(fit.residuals, E, atol=1e-15)
    # Event window L = 2, rm* = (0.01, 0.03): g = (2, 0.04); g'(X'X)^-1 g = 4/5 + 0.0016/0.001 = 2.4
    rm_ev = np.array([0.01, 0.03])
    assert car_variance_market_model(fit, rm_ev) == pytest.approx((1e-5 / 3) * (2 + 2.4), rel=1e-12)
    ri_ev = 0.001 + 1.5 * rm_ev + np.array([0.01, 0.0])  # +1 % abnormal on day 0
    car = float(np.sum(fit.abnormal(ri_ev, rm_ev)))
    assert car == pytest.approx(0.01, abs=1e-15)
    inf = market_model_inference(fit, car, rm_ev)
    se = math.sqrt((1e-5 / 3) * 4.4)
    assert inf.standard_error == pytest.approx(se, rel=1e-12)
    assert inf.test_statistic == pytest.approx(0.01 / se, rel=1e-12)  # = 2.6112
    assert inf.p_value == pytest.approx(2 * stats.t.sf(0.01 / se, 3), rel=1e-10)
    q = stats.t.ppf(0.975, 3)
    assert (inf.ci_low, inf.ci_high) == pytest.approx((0.01 - q * se, 0.01 + q * se), rel=1e-12)
    # estimation error matters: SE exceeds the naive sigma * sqrt(L)
    assert (
        inf.standard_error
        > inf.standard_error_ignoring_estimation_error
        == pytest.approx(math.sqrt(2e-5 / 3))
    )


def _sample(n=130, alpha=0.0004, beta=1.3, sd=0.01, seed=11):
    rng = np.random.default_rng(seed)
    rm = rng.normal(0.0003, 0.011, n)
    return rm, alpha + beta * rm + rng.normal(0, sd, n)


def test_ols_matches_independent_lstsq_and_recovers_known_alpha_beta():
    rm, ri = _sample(n=2000, seed=3)
    fit = fit_market_model(ri, rm, min_obs=100)
    x = np.column_stack([np.ones_like(rm), rm])
    coef, ssr, *_ = np.linalg.lstsq(x, ri, rcond=None)
    assert (fit.alpha, fit.beta) == pytest.approx(tuple(coef), abs=1e-12)
    assert fit.sigma2 == pytest.approx(float(ssr[0]) / (len(ri) - 2), rel=1e-10)
    cov = fit.sigma2 * np.linalg.inv(x.T @ x)
    assert (fit.alpha_se, fit.beta_se) == pytest.approx(
        (math.sqrt(cov[0, 0]), math.sqrt(cov[1, 1])), rel=1e-10
    )
    # recovery within ~4 standard errors of the true parameters
    assert abs(fit.beta - 1.3) < 4 * fit.beta_se and abs(fit.alpha - 0.0004) < 4 * fit.alpha_se


def test_car_variance_equals_full_covariance_matrix_construction():
    rm, ri = _sample()
    fit = fit_market_model(ri, rm, min_obs=100)
    rm_ev = np.array([0.012, -0.004, 0.02, 0.0, -0.01, 0.007])
    xs = np.column_stack([np.ones_like(rm_ev), rm_ev])
    x = np.column_stack([np.ones_like(rm), rm])
    v = fit.sigma2 * (np.eye(6) + xs @ np.linalg.inv(x.T @ x) @ xs.T)  # Var(AR*) (L x L)
    assert car_variance_market_model(fit, rm_ev) == pytest.approx(
        float(np.ones(6) @ v @ np.ones(6)), rel=1e-12
    )
    np.testing.assert_allclose(fit.ar_variance(rm_ev), np.diag(v), rtol=1e-12)


def test_zero_abnormal_return_under_exact_model_and_undefined_test():
    rm = np.linspace(-0.02, 0.02, 120)
    ri = 0.0005 + 1.2 * rm  # exactly the model: no noise
    fit = fit_market_model(ri, rm, min_obs=100)
    rm_ev = np.array([0.01, -0.005])
    ar = fit.abnormal(0.0005 + 1.2 * rm_ev, rm_ev)
    np.testing.assert_allclose(ar, 0.0, atol=1e-12)
    inf = market_model_inference(fit, float(ar.sum()), rm_ev)
    assert inf.status == InferenceStatus.NOT_AVAILABLE  # sigma^2 = 0: a t-statistic is undefined


@pytest.mark.parametrize("effect", [0.05, -0.04])
def test_injected_event_effect_is_recovered(effect):
    rm, ri = _sample(sd=0.002, seed=21)
    fit = fit_market_model(ri, rm, min_obs=100)
    rng = np.random.default_rng(99)
    rm_ev = rng.normal(0, 0.01, 2)
    ri_ev = 0.0004 + 1.3 * rm_ev + rng.normal(0, 0.002, 2) + np.array([effect, 0.0])
    car = float(fit.abnormal(ri_ev, rm_ev).sum())
    inf = market_model_inference(fit, car, rm_ev)
    assert abs(car - effect) < 0.01  # noise sd ~ 0.003 for two sessions
    assert inf.p_value < 1e-6 and inf.ci_low < effect < inf.ci_high
    assert math.copysign(1, inf.test_statistic) == math.copysign(1, effect)


def test_monte_carlo_size_and_coverage_under_the_null():
    """Seeded simulation: under H0 the 5 % test rejects ~5 % and the 95 % CI covers ~95 %."""
    rng = np.random.default_rng(20261102)
    n_sim, rejections, covered = 1500, 0, 0
    for _ in range(n_sim):
        rm = rng.normal(0, 0.01, 133)
        ri = 0.0002 + 0.9 * rm + rng.normal(0, 0.015, 133)
        fit = fit_market_model(ri[:130], rm[:130], min_obs=100)
        car = float(fit.abnormal(ri[130:], rm[130:]).sum())
        inf = market_model_inference(fit, car, rm[130:])
        rejections += inf.p_value < 0.05
        covered += inf.ci_low <= 0.0 <= inf.ci_high
    se = math.sqrt(0.05 * 0.95 / n_sim)  # binomial standard error ~0.0056
    assert abs(rejections / n_sim - 0.05) < 4 * se
    assert abs(covered / n_sim - 0.95) < 4 * se


def test_market_adjusted_is_a_difference_with_known_variance():
    rm, ri = _sample()
    fit = fit_market_adjusted(ri, rm, min_obs=100)
    d = ri - rm
    assert (
        fit.mean == pytest.approx(d.mean()) and fit.sigma2 == pytest.approx(d.var(ddof=1)) and fit.df == 129
    )
    inf = market_adjusted_inference(fit, 0.03, 3)
    assert inf.standard_error == pytest.approx(math.sqrt(3 * d.var(ddof=1)))
    assert inf.p_value == pytest.approx(2 * stats.t.sf(0.03 / math.sqrt(3 * d.var(ddof=1)), 129))


def test_unstable_or_insufficient_estimation_is_rejected():
    with pytest.raises(UnstableEstimation, match="observations"):
        fit_market_model(np.ones(50), np.linspace(0, 1, 50), min_obs=100)
    with pytest.raises(UnstableEstimation, match="no variation"):
        fit_market_model(np.random.default_rng(1).normal(size=120), np.full(120, 0.001), min_obs=100)


def test_placebo_distribution_is_bounded_and_extreme_values_get_minimum_p():
    rm, ri = _sample()
    fit = fit_market_model(ri, rm, min_obs=100)
    offs = list(range(-150, -20))
    p = placebo_distribution(fit.residuals, offs, 2, 10.0)
    assert p["status"] == "OK" and p["placebo_windows"] == 129
    assert p["empirical_p_value"] == pytest.approx(1 / 130)
    p0 = placebo_distribution(fit.residuals, offs, 2, 0.0)
    assert p0["empirical_p_value"] == 1.0
    gapped = [o for o in offs if o % 3]  # gaps break contiguity: fewer complete windows
    assert placebo_distribution(fit.residuals[: len(gapped)], gapped, 3, 0.0)["status"] == "NOT_AVAILABLE"


# ---------------------------------------------------------------------------- aggregation & labels
def _item(eid, day0, sessions, car, subtype="CPI", t="2026-10-14T08:30:00-04:00"):
    return {
        "event_id": eid, "source_name": "BLS via ALFRED", "event_type": "MACRO_RELEASE", "event_subtype": subtype,
        "alignment": {"day0": day0, "event_time": t}, "session_span": sessions,
        "windows": [{"window": "[0,+1]", "start": 0, "end": 1, "status": "VALID", "car": car,
                     "raw_cumulative_return": car, "bhar": None}],
    }  # fmt: skip


def test_same_announcement_counts_once_and_overlaps_merge():
    items = [
        _item("EV-cpi", "2026-10-14", ["2026-10-14", "2026-10-15"], 0.01),
        _item("EV-core", "2026-10-14", ["2026-10-14", "2026-10-15"], 0.01, "CORE_CPI"),
        _item(
            "EV-nfp",
            "2026-10-15",
            ["2026-10-15", "2026-10-16"],
            -0.02,
            "NONFARM_PAYROLLS",
            "2026-10-15T08:30",
        ),
        _item("EV-later", "2026-11-12", ["2026-11-12", "2026-11-13"], 0.04, t="2026-11-12T08:30"),
    ]
    clusters = agg.build_clusters(items, (0, 1))
    assert len(clusters) == 2
    first = clusters[0]
    assert first["overlapping"] and len(first["announcements"]) == 2
    assert sorted(first["announcements"][0]["event_ids"] + first["announcements"][1]["event_ids"]) == [
        "EV-core", "EV-cpi", "EV-nfp"]  # fmt: skip
    a = agg.aggregate(clusters, ["[0,+1]"])["[0,+1]"]
    assert a["car"]["n"] == 2 and a["car"]["mean"] == pytest.approx((((0.01 - 0.02) / 2) + 0.04) / 2)
    assert a["cross_sectional_test"]["status"] == "NOT_REPORTED"


def test_cross_sectional_test_matches_scipy_and_is_limited():
    vals = list(np.random.default_rng(5).normal(0.002, 0.01, 12))
    t = agg.cross_sectional_test(vals)
    ref = stats.ttest_1samp(vals, 0.0)
    assert t["status"] == "LIMITED" and t["test_statistic"] == pytest.approx(ref.statistic, rel=1e-12)
    assert t["p_value"] == pytest.approx(ref.pvalue, rel=1e-10)


def test_synthetic_origin_dominates():
    assert origin_of(["fixture:real_snapshot", "fixture:synthetic"]) == ("fixture:synthetic", True)
    assert origin_of(["fixture:real_snapshot"]) == ("fixture:real_snapshot", False)
    assert origin_of(["live", "fixture:real_snapshot"])[0].startswith("mixed:")


# ---------------------------------------------------------------------------- yields
def test_yield_day0_uses_the_1530_quote_time_and_no_quote_days():
    days = [date(2025, 9, 16), date(2025, 9, 17), date(2025, 9, 18), date(2025, 10, 14)]
    fomc = align(ny(2025, 9, 17, 14, 0))
    assert yield_day0(fomc, days)[0] == date(2025, 9, 17)
    late = align(ny(2025, 9, 17, 16, 5))
    assert yield_day0(late, days)[0] == date(2025, 9, 18)
    at = align(ny(2025, 9, 17, 15, 30))
    assert yield_day0(at, days)[0] == date(2025, 9, 18)
    columbus = align_timestamp("EV-x", ny(2025, 10, 13, 8, 30), CAL, basis=VB)  # no yield quote that day
    assert yield_day0(columbus, days)[0] == date(2025, 10, 14)
