"""Form validation and data-origin labelling (pure functions)."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from finance_agent.timeutil import ET, parse_as_of
from finance_agent.ui import labels, validation
from finance_agent.ui.validation import ValidationError


@pytest.mark.parametrize("raw,expected", [("msft", "MSFT"), (" brk.b ", "BRK.B"), ("BF-B", "BF-B")])
def test_ticker_normalized(raw, expected):
    assert validation.ticker(raw) == expected


@pytest.mark.parametrize("raw", ["1BAD", "TOOLONGTICKER", "MS FT", "<script>", ""])
def test_ticker_rejected(raw):
    with pytest.raises(ValidationError):
        validation.ticker(raw)


def test_optional_ticker_may_be_blank():
    assert validation.ticker("  ", required=False) is None


@pytest.mark.parametrize("q", [1, 21, 0])
def test_quarters_bounds(q):
    with pytest.raises(ValidationError):
        validation.quarters(q)


def test_cutoff_date_only_keeps_backend_end_of_day_rule():
    s = validation.cutoff(date(2026, 10, 28))
    assert s == "2026-10-28"
    assert parse_as_of(s).astimezone(ET).isoformat().startswith("2026-10-28T23:59:59")


def test_cutoff_with_time_is_new_york_wall_clock():
    s = validation.cutoff(date(2026, 10, 28), time(8, 30))
    dt = datetime.fromisoformat(s)
    assert dt.utcoffset().total_seconds() == -4 * 3600  # EDT on 2026-10-28
    assert parse_as_of(s) == dt


def test_no_cutoff():
    assert validation.cutoff(None) is None


def test_date_range_order():
    with pytest.raises(ValidationError):
        validation.date_range(date(2026, 10, 2), date(2026, 10, 1))
    assert validation.date_range(date(2026, 10, 1), date(2026, 10, 1))


def test_ids():
    assert validation.event_id("EV-0123456789abcdef") == "EV-0123456789abcdef"
    with pytest.raises(ValidationError):
        validation.event_id("EV-xyz")
    with pytest.raises(ValidationError):
        validation.run_id("../../etc/passwd")


# ---------------------------------------------------------------- origin labels


def test_synthetic_fixture_is_labelled_synthetic():
    lab = labels.origin_label("fixture:synthetic")
    assert lab.synthetic and lab.kind == "error" and "SYNTHETIC" in lab.text and "NOT real" in lab.text


def test_mixed_with_synthetic_is_never_presented_as_real():
    for origins in ("mixed:fixture:real_snapshot,fixture:synthetic", ["live", "fixture:synthetic"]):
        lab = labels.origin_label(origins)
        assert lab.synthetic and "SYNTHETIC" in lab.text and "MIXED" in lab.text


def test_real_snapshot_and_live_are_distinct():
    snap, live = labels.origin_label("fixture:real_snapshot"), labels.origin_label("live")
    assert "SNAPSHOT" in snap.text and not snap.synthetic
    assert live.text.startswith("LIVE") and live.kind == "success"
    assert labels.origin_label(["live", "fixture:real_snapshot"]).text.startswith("MIXED")


@pytest.mark.parametrize("origin", [None, "", [], "unknown", "something-else"])
def test_missing_or_unknown_origin_is_flagged(origin):
    lab = labels.origin_label(origin)
    assert lab.kind == "warning" and "UNVERIFIED" in lab.text


def test_verification_kinds():
    assert labels.verification_kind("passed") == "success"
    assert labels.verification_kind("failed") == "error"
    assert labels.verification_kind(None) == "warning"
