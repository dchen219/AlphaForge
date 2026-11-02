"""Canonical number formatting shared by reports and the numeric-claim verifier."""

from __future__ import annotations


def fmt_usd(v: float | None) -> str:
    if v is None:
        return "n/a"
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.1f}M"
    return f"{sign}${a:,.0f}"


def fmt_pct(r: float | None) -> str:
    return "n/a" if r is None else f"{r * 100:.1f}%"


def fmt_pp(d: float | None) -> str:
    return "n/a" if d is None else f"{d * 100:+.1f} pp"


def fmt_multiple(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}x"


def fmt_value(v: float | None, unit: str) -> str:
    if unit == "USD":
        return fmt_usd(v)
    if unit == "ratio":
        return fmt_pct(v)
    if unit == "ratio_change":
        return fmt_pp(v)
    if unit == "multiple":
        return fmt_multiple(v)
    return "n/a" if v is None else f"{v:,.4g}"
