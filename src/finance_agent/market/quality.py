"""Market-data quality checks (flag, explain or quarantine — never silently delete or fill).

* Record-level checks run before storage: invalid records are stored QUARANTINED (kept for
  audit, excluded from queries and calculations).
* Series-level checks run after storage on the visible series: missing sessions (classified),
  extreme moves (WARNING, kept: they may be real), split-like discontinuities with or without
  a recorded corporate action, and adjustment-status inconsistencies.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

from finance_agent.market.calendar import TradingCalendar
from finance_agent.market.schemas import (
    AdjustmentBasis,
    BarStatus,
    CorporateAction,
    CorporateActionType,
    DataQualityIssue,
    GapKind,
    PriceBar,
    QualityCheck,
    QualityStatus,
)
from finance_agent.timeutil import ET

COMMON_SPLIT_RATIOS = (2, 3, 4, 5, 8, 10, 15, 20, 1 / 2, 1 / 3, 1 / 4, 1 / 5, 1 / 8, 1 / 10, 1 / 20)
OHLC_TOLERANCE = 1e-6  # relative tolerance for OHLC containment


@dataclass
class QualityConfig:
    extreme_return_threshold: float = 0.25
    split_match_tolerance: float = 0.12  # |ratio × r − 1| within this → matches a split of ratio r


@dataclass
class RecordCheck:
    status: QualityStatus = QualityStatus.OK
    issues: list[DataQualityIssue] = field(default_factory=list)


def _issue(
    check, severity, bar: PriceBar | None, detail: str, status: str = "OPEN", **kw
) -> DataQualityIssue:
    return DataQualityIssue(
        check=check,
        severity=severity,
        instrument_id=bar.instrument_id if bar else kw.get("instrument_id"),
        provider=bar.provider if bar else kw.get("provider"),
        session_date=bar.session_date if bar else kw.get("session_date"),
        detail=detail,
        status=status,
        run_id=kw.get("run_id"),
    )


def check_record(
    bar: PriceBar, calendar: TradingCalendar, missing_fields: tuple[str, ...] = ()
) -> RecordCheck:
    out = RecordCheck()

    def bad(check, detail):
        out.status = QualityStatus.QUARANTINED
        out.issues.append(_issue(check, "ERROR", bar, detail, "QUARANTINED"))

    if missing_fields:
        bad(QualityCheck.MISSING_FIELD, f"missing required field(s): {', '.join(missing_fields)}")
    prices = {"open": bar.open, "high": bar.high, "low": bar.low, "close": bar.close}
    for k, v in prices.items():
        if v is not None and v <= 0:
            bad(QualityCheck.NONPOSITIVE_PRICE, f"{k} = {v}")
    if bar.volume is not None and bar.volume < 0:
        bad(QualityCheck.NEGATIVE_VOLUME, f"volume = {bar.volume}")
    if None not in prices.values():
        hi, lo = bar.high, bar.low
        if hi < lo:
            bad(QualityCheck.HIGH_BELOW_LOW, f"high {hi} < low {lo}")
        else:
            tol = OHLC_TOLERANCE * hi
            for k in ("open", "close"):
                if not (lo - tol <= prices[k] <= hi + tol):
                    bad(QualityCheck.OHLC_INCONSISTENT, f"{k} {prices[k]} outside [low {lo}, high {hi}]")
    local = bar.bar_timestamp.astimezone(ET)
    if local.date() != bar.session_date or (local.hour, local.minute) != (0, 0):
        out.issues.append(
            _issue(
                QualityCheck.WRONG_TIMEZONE,
                "WARNING",
                bar,
                f"daily bar timestamp {bar.bar_timestamp.isoformat()} is not 00:00 America/New_York "
                f"of session {bar.session_date}",
            )
        )
    if not calendar.is_session(bar.session_date):
        bad(
            QualityCheck.NON_SESSION_BAR,
            f"bar dated {bar.session_date} ({bar.session_date.strftime('%A')}) — not a {calendar.name} session",
        )
    return out


def duplicates(bars: list[PriceBar]) -> tuple[list[PriceBar], list[DataQualityIssue]]:
    """Within one provider response: identical duplicates collapse (INFO); conflicting duplicates
    are all quarantined (ERROR) — neither value is chosen."""
    by: dict[tuple, list[PriceBar]] = defaultdict(list)
    for b in bars:
        by[(b.instrument_id, b.session_date, str(b.adjustment))].append(b)
    out, issues = [], []
    for group in by.values():
        if len(group) == 1:
            out.append(group[0])
            continue
        if len({b.values() for b in group}) == 1:
            out.append(group[0])
            issues.append(
                _issue(
                    QualityCheck.DUPLICATE_BAR, "INFO", group[0], f"{len(group)} identical copies collapsed"
                )
            )
        else:
            for b in group:
                out.append(b.model_copy(update={"quality_status": QualityStatus.QUARANTINED}))
            issues.append(
                _issue(
                    QualityCheck.DUPLICATE_BAR,
                    "ERROR",
                    group[0],
                    f"{len(group)} conflicting bars for one session; all quarantined",
                    "QUARANTINED",
                )
            )
    return out, issues


def classify_gaps(
    expected: list[date],
    visible: dict[date, PriceBar],
    stored_status: dict[date, set],
    failed: bool,
) -> dict[date, GapKind]:
    gaps = {}
    for d in expected:
        if d in visible:
            continue
        states = stored_status.get(d, set())
        if failed:
            gaps[d] = GapKind.PROVIDER_FAILURE
        elif any(st == BarStatus.PROVISIONAL for st, _ in states):
            gaps[d] = GapKind.NOT_FINALIZED
        elif any(qs == QualityStatus.QUARANTINED for _, qs in states):
            gaps[d] = GapKind.QUARANTINED
        else:
            gaps[d] = GapKind.MISSING_DATA
    return gaps


def _split_like(ratio: float, tol: float) -> float | None:
    for r in COMMON_SPLIT_RATIOS:
        if abs(ratio * r - 1.0) <= tol:
            return r
    return None


def series_checks(
    instrument_id: str,
    provider: str,
    basis: AdjustmentBasis,
    sessions: list[date],
    visible: dict[date, PriceBar],
    actions: list[CorporateAction],
    cfg: QualityConfig,
    run_id: str | None = None,
) -> list[DataQualityIssue]:
    """Return-based checks over consecutive sessions with visible closes."""
    issues: list[DataQualityIssue] = []
    splits = {
        a.ex_date: a.split_ratio
        for a in actions
        if a.action_type == CorporateActionType.STOCK_SPLIT and a.ex_date
    }
    for prev, cur in itertools.pairwise(sessions):
        a, b = visible.get(prev), visible.get(cur)
        if a is None or b is None or not a.close or not b.close:
            continue
        ratio = b.close / a.close
        r = splits.get(cur)
        bar = b
        if basis == AdjustmentBasis.RAW and r and abs(r - 1) > 0.05:
            if abs(ratio * r - 1.0) <= 0.5:
                issues.append(
                    _issue(
                        QualityCheck.CORPORATE_ACTION_DISCONTINUITY,
                        "INFO",
                        bar,
                        f"raw close ratio {ratio:.4f} on split ex-date (ratio {r:g}); explained by the "
                        "recorded split - use split_adjusted or total_return for returns",
                        "EXPLAINED",
                        run_id=run_id,
                    )
                )
            elif abs(ratio - 1.0) < 0.5 * abs(1 - 1 / r):
                issues.append(
                    _issue(
                        QualityCheck.INCONSISTENT_ADJUSTMENT,
                        "WARNING",
                        bar,
                        f"series labelled raw shows no discontinuity (ratio {ratio:.4f}) at split ex-date "
                        f"(ratio {r:g}); data may already be split-adjusted",
                        run_id=run_id,
                    )
                )
            continue
        if (
            basis != AdjustmentBasis.RAW
            and r
            and abs(r - 1) > 0.05
            and abs(ratio * r - 1.0) <= cfg.split_match_tolerance
        ):
            issues.append(
                _issue(
                    QualityCheck.INCONSISTENT_ADJUSTMENT,
                    "WARNING",
                    bar,
                    f"{basis} series shows a split-sized jump (ratio {ratio:.4f}) at ex-date; possible "
                    "missing or double adjustment",
                    run_id=run_id,
                )
            )
            continue
        move = ratio - 1.0
        if abs(move) <= cfg.extreme_return_threshold:
            continue
        like = _split_like(ratio, cfg.split_match_tolerance)
        if like is not None:
            issues.append(
                _issue(
                    QualityCheck.UNEXPLAINED_DISCONTINUITY,
                    "WARNING",
                    bar,
                    f"close ratio {ratio:.4f} resembles a {like:g}-for-1 split but no corporate action "
                    "is recorded; not adjusted, not deleted",
                    run_id=run_id,
                )
            )
        else:
            issues.append(
                _issue(
                    QualityCheck.EXTREME_RETURN,
                    "WARNING",
                    bar,
                    f"daily move {move:+.2%} exceeds +/-{cfg.extreme_return_threshold:.0%} threshold; kept "
                    "(may be genuine), flagged for review",
                    run_id=run_id,
                )
            )
    return issues


def gap_issues(
    instrument_id: str, provider: str, gaps: dict[date, GapKind], run_id: str | None = None
) -> list[DataQualityIssue]:
    sev = {
        GapKind.MISSING_DATA: "WARNING",
        GapKind.PROVIDER_FAILURE: "ERROR",
        GapKind.NOT_FINALIZED: "INFO",
        GapKind.QUARANTINED: "ERROR",
    }
    return [
        _issue(
            QualityCheck.PROVISIONAL_BAR if kind == GapKind.NOT_FINALIZED else QualityCheck.MISSING_SESSION,
            sev.get(kind, "WARNING"),
            None,
            f"{kind}: no usable bar for trading session {d}"
            + (
                " (a full-day trading halt cannot be distinguished without halt data)"
                if kind == GapKind.MISSING_DATA
                else ""
            ),
            instrument_id=instrument_id,
            provider=provider,
            session_date=d,
            run_id=run_id,
        )
        for d, kind in sorted(gaps.items())
    ]
