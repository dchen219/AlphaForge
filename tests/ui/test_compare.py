"""Pure comparison / point-in-time integrity helpers (SYNTHETIC rows constructed inline)."""

from __future__ import annotations

from datetime import UTC, datetime

from finance_agent.ui import compare
from finance_agent.ui.validation import ValidationError, market_range

CUT = datetime(2026, 10, 1, 3, 59, 59, tzinfo=UTC)


def test_rows_violations_flags_only_later_records():
    rows = [
        {"session_date": "2026-09-29", "available_at": "2026-09-30T04:00:00+00:00"},
        {"session_date": "2026-09-30", "available_at": "2026-10-01T04:00:00+00:00"},  # after the cutoff
        {"session_date": "2026-09-28", "gap": "MISSING_DATA"},  # no availability: not a violation
    ]
    bad = compare.rows_violations(rows, CUT, "bar")
    assert len(bad) == 1 and "2026-09-30" in bad[0]
    assert compare.rows_violations(rows, None, "bar") == []


def test_diff_rows_aligns_on_key_without_arithmetic():
    a = [{"d": "1", "v": 1.0}, {"d": "2", "v": 2.0}]
    b = [{"d": "2", "v": 2.5}, {"d": "3", "v": None}]
    rows = {r["d"]: r for r in compare.diff_rows(a, b, key="d", value="v")}
    assert rows["1"]["change"] == "only at A"
    assert rows["2"]["change"] == "different" and rows["2"]["v at B"] == 2.5
    assert rows["3"]["change"] == "missing at both"  # null observation is reported, not filled
    assert compare.summarize(list(rows.values())) == {"only at A": 1, "different": 1, "missing at both": 1}


def test_market_range_validation():
    from datetime import date

    assert market_range(date(2026, 1, 1), date(2026, 2, 1)) == (date(2026, 1, 1), date(2026, 2, 1))
    for args in ((None, date(2026, 1, 1)), (date(2026, 2, 1), date(2026, 1, 1))):
        try:
            market_range(*args)
        except ValidationError:
            continue
        raise AssertionError(args)
    try:
        market_range(date(2020, 1, 1), date(2026, 1, 1))
    except ValidationError as exc:
        assert "limited" in str(exc)
    else:
        raise AssertionError("long range accepted")


def test_display_rows_stringifies_mixed_columns_only():
    from finance_agent.ui.components import display_rows

    rows = [{"rank": 1, "v": 1.5, "x": {"a": 1}}, {"rank": "—", "v": None, "x": None}]
    out = display_rows(rows)
    assert [r["rank"] for r in out] == ["1", "—"]
    assert [r["v"] for r in out] == [1.5, None]  # numeric column untouched
    assert out[0]["x"] == "{'a': 1}"
