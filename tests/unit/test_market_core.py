"""v0.3-A market-data core: calendars, adjustment, calculations, parsing, quality.

Calendar facts are REAL (exchange_calendars XNYS). Everything else uses SYNTHETIC inline inputs
with hand-calculated expected values.
"""

from __future__ import annotations

import math
import statistics
from datetime import UTC, date, datetime

import pytest

from finance_agent.config.logging import redact
from finance_agent.errors import DataUnavailableError, MalformedDataError
from finance_agent.market import calculations as calc
from finance_agent.market.adjust import factors_for, reconcile, split_factors, total_return_factors
from finance_agent.market.calendar import get_calendar
from finance_agent.market.providers import parse_alpaca_actions, parse_alpaca_bars
from finance_agent.market.quality import QualityConfig, check_record, duplicates, series_checks
from finance_agent.market.schemas import (
    AdjustmentBasis,
    CorporateAction,
    CorporateActionType,
    PriceBar,
    QualityCheck,
    QualityStatus,
    SessionPhase,
)
from finance_agent.market.yields import federal_holidays_observed, first_vintage_rule, is_publication_day
from finance_agent.storage.snapshots import SnapshotRef
from finance_agent.timeutil import ET

CAL = get_calendar()
REF = SnapshotRef(1, "c" * 64, datetime(2026, 1, 1, tzinfo=UTC), "fixture:synthetic", "https://x.test", False)
T0 = datetime(2026, 1, 1, tzinfo=UTC)


# ------------------------------------------------------------------ trading calendar (REAL rules)


@pytest.mark.parametrize(
    "d,is_session",
    [
        (date(2025, 1, 9), False),  # national day of mourning (special closure)
        (date(2025, 11, 27), False),  # Thanksgiving
        (date(2026, 4, 3), False),  # Good Friday
        (date(2026, 7, 3), False),  # Independence Day (Sat) observed Friday
        (date(2026, 6, 19), False),  # Juneteenth
        (date(2026, 10, 31), False),  # Saturday
        (date(2026, 11, 2), True),
    ],
)
def test_holidays_weekends_and_special_closures(d, is_session):
    assert CAL.is_session(d) is is_session


def test_early_close_and_dst_boundaries_are_real_instants():
    s = CAL.session(date(2025, 11, 28))  # day after Thanksgiving: 13:00 ET close
    assert s.early_close and s.market_close == datetime(2025, 11, 28, 18, 0, tzinfo=UTC)
    assert s.after_hours_close == datetime(2025, 11, 28, 22, 0, tzinfo=UTC)
    # US DST starts 2026-03-08: the 09:30 ET open moves from 14:30Z to 13:30Z.
    assert CAL.session(date(2026, 3, 6)).market_open == datetime(2026, 3, 6, 14, 30, tzinfo=UTC)
    assert CAL.session(date(2026, 3, 9)).market_open == datetime(2026, 3, 9, 13, 30, tzinfo=UTC)
    # ... and back on 2026-11-01: 16:00 ET close is 20:00Z before, 21:00Z after.
    assert CAL.session(date(2026, 10, 30)).market_close == datetime(2026, 10, 30, 20, 0, tzinfo=UTC)
    assert CAL.session(date(2026, 11, 2)).market_close == datetime(2026, 11, 2, 21, 0, tzinfo=UTC)


def test_previous_and_next_session_skip_holidays():
    assert CAL.previous_session(date(2025, 1, 10)) == date(2025, 1, 8)  # skips 2025-01-09 closure
    assert CAL.next_session(date(2026, 4, 2)) == date(2026, 4, 6)  # skips Good Friday + weekend
    with pytest.raises(DataUnavailableError):
        CAL.session(date(2026, 4, 3))


@pytest.mark.parametrize(
    "local,phase,event_session,first",
    [
        (datetime(2026, 10, 28, 8, 30), SessionPhase.PRE_MARKET, None, date(2026, 10, 28)),
        (datetime(2026, 10, 28, 10, 0), SessionPhase.REGULAR, date(2026, 10, 28), date(2026, 10, 29)),
        (
            datetime(2026, 10, 28, 16, 0),
            SessionPhase.AFTER_HOURS,
            None,
            date(2026, 10, 29),
        ),  # close exclusive
        (datetime(2026, 10, 28, 16, 5), SessionPhase.AFTER_HOURS, None, date(2026, 10, 29)),
        (datetime(2026, 10, 28, 21, 0), SessionPhase.CLOSED, None, date(2026, 10, 29)),
        (datetime(2026, 10, 28, 2, 0), SessionPhase.CLOSED, None, date(2026, 10, 28)),
        (datetime(2025, 11, 28, 13, 30), SessionPhase.AFTER_HOURS, None, date(2025, 12, 1)),  # early close
        (datetime(2026, 11, 26, 10, 0), SessionPhase.NON_TRADING_DAY, None, date(2026, 11, 27)),
    ],
)
def test_event_alignment(local, phase, event_session, first):
    a = CAL.align(local.replace(tzinfo=ET))
    assert (a.phase, a.event_session, a.first_reaction_session) == (phase, event_session, first)


def test_after_close_event_is_never_assigned_to_that_days_regular_session():
    """NEGATIVE: an earnings release at 16:05 ET must not be attributed to the same-day bar."""
    a = CAL.align(datetime(2026, 10, 28, 20, 5, tzinfo=UTC))  # 16:05 EDT
    assert a.event_session is None and a.first_reaction_session == date(2026, 10, 29)
    with pytest.raises(ValueError):
        CAL.align(datetime(2026, 10, 28, 16, 5))  # naive timestamps are rejected


# ------------------------------------------------------------------ corporate actions + adjustment


def action(kind, ex, **kw) -> CorporateAction:
    return CorporateAction(
        action_id=kw.pop("action_id", f"A-{kind}-{ex}"),
        provider="synthetic",
        instrument_id="SYN",
        action_type=kind,
        ex_date=ex,
        retrieved_at=T0,
        available_at=T0,
        **kw,
    )


def test_invalid_corporate_actions_are_rejected():
    with pytest.raises(ValueError):
        action(CorporateActionType.STOCK_SPLIT, date(2024, 6, 10), old_rate=0, new_rate=10)
    with pytest.raises(ValueError):
        action(CorporateActionType.CASH_DIVIDEND, date(2024, 6, 10), cash_amount=-1)
    with pytest.raises(ValueError):
        action(CorporateActionType.STOCK_SPLIT, None, old_rate=1, new_rate=2)


def test_split_factors_forward_and_reverse():
    fs = split_factors(
        [
            action(CorporateActionType.STOCK_SPLIT, date(2024, 6, 10), old_rate=1, new_rate=10),
            action(CorporateActionType.STOCK_SPLIT, date(2025, 1, 2), old_rate=4, new_rate=1),  # 1-for-4
        ]
    )
    assert fs.price_multiplier(date(2024, 6, 7)) == pytest.approx(0.1 * 4)
    assert fs.price_multiplier(date(2024, 6, 10)) == pytest.approx(4.0)  # ex-date itself: post-split
    assert fs.price_multiplier(date(2025, 1, 2)) == 1.0
    assert fs.volume_multiplier(date(2024, 6, 7)) == pytest.approx(10 * 0.25)


def test_total_return_dividend_factor_hand_calculated():
    closes = {date(2025, 3, 13): 100.0, date(2025, 3, 14): 99.5}
    fs = total_return_factors(
        [action(CorporateActionType.CASH_DIVIDEND, date(2025, 3, 14), cash_amount=1.0)], closes
    )
    (f,) = fs.factors
    assert f.reference_session == date(2025, 3, 13) and f.reference_close == 100.0
    assert f.price_factor == pytest.approx(0.99)  # 1 - 1.00 / 100.00
    assert fs.price_multiplier(date(2025, 3, 13)) == pytest.approx(0.99)
    assert fs.price_multiplier(date(2025, 3, 14)) == 1.0


def test_dividend_reference_close_is_split_consistent():
    """Split on 06-10 and dividend ex 06-10 (per post-split share): reference close is /10."""
    closes = {date(2024, 6, 7): 1000.0}
    fs = total_return_factors(
        [
            action(CorporateActionType.STOCK_SPLIT, date(2024, 6, 10), old_rate=1, new_rate=10),
            action(CorporateActionType.CASH_DIVIDEND, date(2024, 6, 10), cash_amount=1.0),
        ],
        closes,
    )
    div = next(f for f in fs.factors if f.basis == AdjustmentBasis.TOTAL_RETURN)
    assert div.reference_close == pytest.approx(100.0) and div.price_factor == pytest.approx(0.99)


def test_dividend_without_prior_close_or_too_large_is_not_applied():
    fs = total_return_factors(
        [action(CorporateActionType.CASH_DIVIDEND, date(2025, 1, 2), cash_amount=1)], {}
    )
    assert not fs.factors and "no raw close" in fs.notes[0]
    fs = total_return_factors(
        [action(CorporateActionType.CASH_DIVIDEND, date(2025, 1, 3), cash_amount=5)], {date(2025, 1, 2): 4.0}
    )
    assert not fs.factors and "not below" in fs.notes[0]


def test_provider_adjusted_basis_is_never_re_adjusted():
    with pytest.raises(ValueError):
        factors_for(AdjustmentBasis.PROVIDER_SPLIT, [], {})
    assert factors_for(AdjustmentBasis.RAW, [action(CorporateActionType.STOCK_SPLIT, date(2024, 6, 10),
                                                    old_rate=1, new_rate=10)], {}).factors == []  # fmt: skip


def test_reconcile_reports_mismatches():
    d1, d2 = date(2024, 6, 6), date(2024, 6, 7)
    ok = reconcile({d1: 62.791, d2: 59.073}, {d1: 62.791, d2: 59.0730001})
    assert ok["within_tolerance"] and ok["sessions_compared"] == 2
    bad = reconcile({d1: 627.91}, {d1: 62.791})  # double/missing adjustment shows as a 10x gap
    assert not bad["within_tolerance"] and bad["mismatched_sessions"] == 1


# ------------------------------------------------------------------ calculations (hand-calculated)

S = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)]


def test_simple_and_log_returns_and_cumulative():
    v = {S[0]: 100.0, S[1]: 110.0, S[2]: 99.0, S[3]: 99.0}
    r = calc.simple_returns(v, S)
    assert [p.value for p in r] == pytest.approx([0.10, -0.10, 0.0])
    lr = calc.log_returns(v, S)
    assert lr[0].value == pytest.approx(math.log(1.1)) and lr[1].value == pytest.approx(math.log(0.9))
    cum, gaps = calc.cumulative_return(r)
    assert cum == pytest.approx(99.0 / 100.0 - 1) and gaps == []


def test_missing_session_is_never_bridged():
    """NEGATIVE: a missing price is not replaced with a nearby price."""
    v = {S[0]: 100.0, S[2]: 99.0, S[3]: 100.0}
    r = calc.simple_returns(v, S)
    assert r[0].value is None and r[1].value is None and r[2].value == pytest.approx(100 / 99 - 1)
    cum, gaps = calc.cumulative_return(r)
    assert cum is None and len(gaps) == 2


def test_blocked_session_and_rolling_volatility_and_drawdown():
    v = {d: p for d, p in zip(S, [100.0, 101.0, 99.0, 102.0], strict=True)}
    r = calc.simple_returns(v, S, blocked={S[2]: "split ex-date"})
    assert r[1].value is None and r[1].note == "split ex-date"
    lr = calc.log_returns(v, S)
    vol = calc.rolling_volatility(lr, 3)
    expected = statistics.stdev([math.log(1.01), math.log(99 / 101), math.log(102 / 99)]) * math.sqrt(252)
    assert len(vol) == 1 and vol[0].value == pytest.approx(expected)
    dd, worst = calc.drawdowns(v, S)
    assert worst == pytest.approx(99 / 101 - 1) and dd[-1].value == 0.0


def test_correlation_requires_enough_pairs():
    a = [calc.Point(date(2025, 1, i), float(i)) for i in range(1, 25)]
    b = [calc.Point(date(2025, 1, i), float(2 * i + 1)) for i in range(1, 25)]
    rho, n, note = calc.correlation(a, b)
    assert rho == pytest.approx(1.0) and n == 24 and note is None
    assert calc.correlation(a[:5], b[:5])[0] is None


def test_yield_changes_in_basis_points():
    ch = calc.yield_changes_bp(
        [(date(2025, 10, 10), 4.03), (date(2025, 10, 13), None), (date(2025, 10, 14), 4.05)]
    )
    assert ch[0]["change_bp"] is None
    assert ch[1]["yield_pct"] is None and ch[1]["note"] == "no observation published"
    assert ch[2]["change_bp"] == pytest.approx(2.0) and ch[2]["previous_date"] == date(2025, 10, 10)
    assert calc.yield_changes_bp([(date(2025, 1, 2), 4.25), (date(2025, 1, 3), 4.10)])[1]["change_bp"] == (
        pytest.approx(-15.0)
    )  # 1 percentage point = 100 bp


# ------------------------------------------------------------------ provider parsing (SYNTHETIC)


def test_alpaca_bar_parsing_and_missing_fields():
    payload = {
        "bars": {
            "SYN": [
                {"t": "2025-01-02T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10, "n": 3, "vw": 1.2},
                {"t": "2025-01-03T05:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": None, "v": 10},
                {"t": "2025-01-06T00:00:00", "o": 1, "h": 2, "l": 0.5, "c": 1, "v": 1},  # naive
            ]
        },
        "next_page_token": "abc",
    }
    bars, token = parse_alpaca_bars(payload, "SYN", REF)
    assert token == "abc" and bars[0].timestamp == datetime(2025, 1, 2, 5, tzinfo=UTC)
    assert bars[1].missing_fields == ("c",) and bars[2].timestamp is None
    with pytest.raises(MalformedDataError):
        parse_alpaca_bars({"nope": 1}, "SYN", REF)


def test_alpaca_corporate_action_parsing():
    payload = {
        "corporate_actions": {
            "forward_splits": [
                {"id": "s1", "symbol": "SYN", "old_rate": 1, "new_rate": 10, "ex_date": "2024-06-10"}
            ],
            "cash_dividends": [{"id": "d1", "symbol": "SYN", "rate": 0.25, "ex_date": "2025-03-14"}],
            "reverse_splits": [
                {"id": "x", "symbol": "OTHER", "old_rate": 10, "new_rate": 1, "ex_date": "2025-01-02"}
            ],
            "unknown_kind": [{"id": "u", "symbol": "SYN"}],
        }
    }
    acts, _ = parse_alpaca_actions(payload, "SYN", REF)
    kinds = {a.action_id: a.action_type for a in acts}
    assert kinds == {"s1": CorporateActionType.STOCK_SPLIT, "d1": CorporateActionType.CASH_DIVIDEND}
    assert next(a for a in acts if a.action_id == "d1").fields["cash_amount"] == 0.25


def test_alpaca_credentials_are_redacted():
    text = "{'APCA-API-KEY-ID': 'PKXXXXX', 'APCA-API-SECRET-KEY': 'very-secret'}"
    assert "PKXXXXX" not in redact(text) and "very-secret" not in redact(text)


# ------------------------------------------------------------------ quality (SYNTHETIC)


def bar(d: date, o=10.0, h=11.0, lo=9.0, c=10.5, v=100.0, ts=None, **kw) -> PriceBar:
    ts = ts or datetime.combine(d, datetime.min.time(), tzinfo=ET).astimezone(UTC)
    return PriceBar(
        instrument_id="SYN", provider="synthetic", provider_symbol="SYN", session_date=d, bar_timestamp=ts,
        open=o, high=h, low=lo, close=c, volume=v, retrieved_at=T0, available_at=T0, **kw,
    )  # fmt: skip


@pytest.mark.parametrize(
    "kw,check",
    [
        ({"c": 0.0, "lo": 0.0}, QualityCheck.NONPOSITIVE_PRICE),
        ({"v": -1.0}, QualityCheck.NEGATIVE_VOLUME),
        ({"h": 8.0}, QualityCheck.HIGH_BELOW_LOW),
        ({"o": 12.0}, QualityCheck.OHLC_INCONSISTENT),
    ],
)
def test_record_checks_quarantine_invalid_bars(kw, check):
    rc = check_record(bar(date(2025, 1, 2), **kw), CAL)
    assert rc.status == QualityStatus.QUARANTINED and check in {i.check for i in rc.issues}


def test_holiday_bar_is_quarantined_not_treated_as_session():
    """NEGATIVE: a bar dated on a holiday is not a trading day."""
    rc = check_record(bar(date(2025, 12, 25)), CAL)
    assert rc.status == QualityStatus.QUARANTINED
    assert QualityCheck.NON_SESSION_BAR in {i.check for i in rc.issues}


def test_wrong_timezone_timestamp_is_flagged():
    # UTC midnight instead of New York midnight: maps to the previous evening in New York.
    rc = check_record(bar(date(2025, 1, 2), ts=datetime(2025, 1, 2, 0, 0, tzinfo=UTC)), CAL)
    assert QualityCheck.WRONG_TIMEZONE in {i.check for i in rc.issues}


def test_duplicates_identical_collapse_conflicting_quarantined():
    d = date(2025, 1, 2)
    out, issues = duplicates([bar(d), bar(d)])
    assert len(out) == 1 and issues[0].severity == "INFO"
    out, issues = duplicates([bar(d), bar(d, c=10.6)])
    assert all(b.quality_status == QualityStatus.QUARANTINED for b in out) and issues[0].severity == "ERROR"


def test_series_checks_extreme_unexplained_and_inconsistent():
    s = [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6), date(2025, 1, 7)]
    vis = {
        s[0]: bar(s[0], c=10.0),
        s[1]: bar(s[1], c=13.5, h=14),
        s[2]: bar(s[2], c=6.75, lo=6),
        s[3]: bar(s[3], c=6.8, lo=6),
    }
    issues = series_checks("SYN", "synthetic", AdjustmentBasis.RAW, s, vis, [], QualityConfig())
    checks = [i.check for i in issues]
    assert QualityCheck.EXTREME_RETURN in checks  # +35 %
    assert QualityCheck.UNEXPLAINED_DISCONTINUITY in checks  # x0.5 looks like a 2-for-1 split
    # Raw-labelled data without a jump at a recorded split ex-date: probably already adjusted.
    split = action(CorporateActionType.STOCK_SPLIT, s[3], old_rate=1, new_rate=2)
    issues = series_checks("SYN", "synthetic", AdjustmentBasis.RAW, s, vis, [split], QualityConfig())
    assert QualityCheck.INCONSISTENT_ADJUSTMENT in [i.check for i in issues]


# ------------------------------------------------------------------ Treasury availability rule


def test_h15_publication_days_and_first_vintage_rule():
    h25 = federal_holidays_observed(2025)
    assert date(2025, 10, 13) in h25 and date(2025, 11, 11) in h25 and date(2025, 11, 27) in h25
    assert not is_publication_day(date(2026, 6, 19))  # Juneteenth (Friday)
    assert not is_publication_day(date(2026, 7, 3))  # July 4 on Saturday: observed Friday (OPM)
    assert not is_publication_day(date(2025, 12, 26))  # executive-order closure
    assert first_vintage_rule(date(2025, 10, 10)) == date(2025, 10, 14)  # Fri -> skips Columbus Day
    assert first_vintage_rule(date(2025, 10, 13)) == date(2025, 10, 15)  # holiday row: one day later
    assert first_vintage_rule(date(2026, 7, 2)) == date(2026, 7, 6)
    assert first_vintage_rule(date(2026, 10, 28)) == date(2026, 10, 29)


def test_alpaca_keys_never_rendered_and_required_live():
    from finance_agent.config.settings import Settings
    from finance_agent.errors import ConfigurationError
    from finance_agent.market.providers import AlpacaProvider

    s = Settings(_env_file=None, alpaca_api_key_id="PKLEAKCHECK", alpaca_api_secret_key="secretLEAKCHECK")
    assert "PKLEAKCHECK" not in repr(s) and "secretLEAKCHECK" not in s.model_dump_json()
    p = AlpacaProvider(None, base_url="https://data.alpaca.markets", key_id=None, secret_key=None)
    with pytest.raises(ConfigurationError):
        p.daily_bars("SPY", date(2025, 1, 2), date(2025, 1, 3), AdjustmentBasis.RAW)
