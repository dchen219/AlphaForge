"""Bounded, idempotent historical daily-price ingestion (manual or scheduler-invoked; no daemon).

Per instrument (errors are isolated and reported, other instruments continue):
1. resolve the provider symbol; choose the range (incremental: re-check the last few stored
   sessions so corrections and finalizations are captured);
2. fetch bars (raw, plus optional provider-adjusted bases for reconciliation) and corporate
   actions through the immutable snapshot store;
3. normalize → record-level quality checks → store a new version only if the observation
   changed (re-ingestion is a no-op);
4. series-level checks on the visible series (gaps, extreme moves, discontinuities).

Availability policy: the provider publishes no finalization time for daily bars, so a bar
for session D is treated as available from D+1 00:00 America/New_York
(DATE_ONLY_CONSERVATIVE). A bar retrieved before that instant is PROVISIONAL (it may still
change) and is never presented as final. Corporate actions carry no announcement date in the
provider schema; they are treated as known from their ex-date 00:00 New York.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta

from finance_agent.errors import FinanceAgentError
from finance_agent.market.calendar import get_calendar
from finance_agent.market.providers import AlpacaProvider, ProviderAction, ProviderBar
from finance_agent.market.quality import (
    QualityConfig,
    check_record,
    classify_gaps,
    duplicates,
    gap_issues,
    series_checks,
)
from finance_agent.market.schemas import (
    AdjustmentBasis,
    AvailabilityPrecision,
    BarStatus,
    CorporateAction,
    DataQualityIssue,
    MarketDataIngestionRun,
    PriceBar,
    QualityCheck,
)
from finance_agent.market.store import MarketStore
from finance_agent.market.universe import DEFAULT_UNIVERSE, default_mappings
from finance_agent.providers.base import CachedFetcher
from finance_agent.storage.snapshots import SnapshotRef
from finance_agent.timeutil import ET, available_at, utcnow

log = logging.getLogger(__name__)
INCREMENTAL_RECHECK_SESSIONS = 5
MAX_RANGE_DAYS = 366 * 30


def fixture_observed(ref: SnapshotRef, fixtures_dir, group: str) -> SnapshotRef:
    """Offline: the time a fixture was (really or, for synthetic data, nominally) retrieved."""
    from finance_agent.events.service import fixture_retrieved_at

    when = fixture_retrieved_at(fixtures_dir, group)
    return replace(ref, retrieved_at=when) if when else ref


def build_provider(ctx) -> AlpacaProvider:
    s = ctx.settings
    return AlpacaProvider(
        CachedFetcher("alpaca", ctx.http, ctx.snapshots),
        base_url=s.alpaca_data_url,
        key_id=s.alpaca_api_key_id.get_secret_value() if s.alpaca_api_key_id else None,
        secret_key=s.alpaca_api_secret_key.get_secret_value() if s.alpaca_api_secret_key else None,
        feed=s.alpaca_feed,
        ttl_recent=timedelta(hours=s.cache_ttl_market_bars_hours),
        offline=ctx.http.is_fixture,
    )


class MarketDataIngestor:
    def __init__(self, ctx, provider=None, store: MarketStore | None = None) -> None:
        self.ctx = ctx
        self.provider = provider or build_provider(ctx)
        self.store = store or MarketStore(ctx.db)
        self.calendar = get_calendar()
        self.cfg = QualityConfig(extreme_return_threshold=ctx.settings.market_extreme_return_threshold)

    # ------------------------------------------------------------------ helpers
    def _observed(self, ref: SnapshotRef) -> SnapshotRef:
        if self.ctx.http.is_fixture:
            return fixture_observed(ref, self.ctx.settings.fixtures_dir, "alpaca/")
        return ref

    def register_universe(self, run_id: str | None = None) -> list[DataQualityIssue]:
        issues = self.store.register(DEFAULT_UNIVERSE, default_mappings(self.provider.name))
        for i in issues:
            i.run_id = run_id
            self.store.add_issue(i)
        return issues

    def normalize(
        self, instrument_id: str, pb: ProviderBar, basis: AdjustmentBasis
    ) -> tuple[PriceBar | None, list[DataQualityIssue]]:
        ref = self._observed(pb.snapshot)
        if pb.timestamp is None:
            issue = DataQualityIssue(
                check=QualityCheck.MISSING_FIELD if "t" in pb.missing_fields else QualityCheck.WRONG_TIMEZONE,
                severity="ERROR",
                instrument_id=instrument_id,
                provider=self.provider.name,
                detail=f"bar without a valid timezone-aware timestamp: {pb.raw.get('t')!r} (not stored)",
            )
            return None, [issue]
        session = pb.timestamp.astimezone(ET).date()
        policy = available_at(session)  # D+1 00:00 America/New_York
        final = ref.retrieved_at >= policy
        bar = PriceBar(
            instrument_id=instrument_id,
            provider=self.provider.name,
            provider_symbol=pb.provider_symbol,
            session_date=session,
            bar_timestamp=pb.timestamp,
            open=pb.open,
            high=pb.high,
            low=pb.low,
            close=pb.close,
            volume=pb.volume,
            vwap=pb.vwap,
            trade_count=pb.trade_count,
            adjustment=basis,
            snapshot_id=ref.snapshot_id,
            snapshot_sha256=ref.sha256,
            data_origin=ref.origin,
            retrieved_at=ref.retrieved_at,
            available_at=policy if final else ref.retrieved_at,
            availability_precision=(
                AvailabilityPrecision.DATE_ONLY_CONSERVATIVE
                if final
                else AvailabilityPrecision.OBSERVED_AT_RETRIEVAL
            ),
            bar_status=BarStatus.FINAL if final else BarStatus.PROVISIONAL,
        )
        rc = check_record(bar, self.calendar, pb.missing_fields)
        bar.quality_status = rc.status
        return bar, rc.issues

    def to_action(self, instrument_id: str, pa: ProviderAction) -> tuple[CorporateAction | None, str | None]:
        ref = self._observed(pa.snapshot)
        f = pa.fields
        ex = f.get("ex_date")
        try:
            return (
                CorporateAction(
                    action_id=pa.action_id,
                    provider=self.provider.name,
                    instrument_id=instrument_id,
                    action_type=pa.action_type,
                    ex_date=ex,
                    record_date=f.get("record_date"),
                    payable_date=f.get("payable_date"),
                    process_date=f.get("process_date"),
                    old_rate=f.get("old_rate"),
                    new_rate=f.get("new_rate"),
                    cash_amount=f.get("cash_amount"),
                    currency=f.get("currency") or "USD",
                    new_symbol=f.get("new_symbol"),
                    snapshot_id=ref.snapshot_id,
                    snapshot_sha256=ref.sha256,
                    data_origin=ref.origin,
                    retrieved_at=ref.retrieved_at,
                    # No announcement date in the provider schema: known from the ex-date (00:00 NY).
                    available_at=(
                        datetime.combine(ex, datetime.min.time(), tzinfo=ET) if ex else ref.retrieved_at
                    ),
                    availability_precision=(
                        AvailabilityPrecision.DATE_ONLY_CONSERVATIVE
                        if ex
                        else AvailabilityPrecision.OBSERVED_AT_RETRIEVAL
                    ),
                    raw=pa.raw,
                ),
                None,
            )
        except ValueError as exc:
            return None, str(exc)

    # ------------------------------------------------------------------ run
    def run(
        self,
        tickers: list[str] | None,
        start: date,
        end: date,
        *,
        bases: tuple[AdjustmentBasis, ...] = (AdjustmentBasis.RAW,),
        include_actions: bool = True,
        incremental: bool = True,
        refresh: bool = False,
        progress: Callable[[str], None] | None = None,
    ) -> MarketDataIngestionRun:
        if end < start:
            raise ValueError("end must not be before start")
        if (end - start).days > MAX_RANGE_DAYS:
            raise ValueError("range too large (bounded ingestion)")
        t0 = time.perf_counter()
        req0 = self.ctx.http.request_count
        run = MarketDataIngestionRun(
            run_id=str(uuid.uuid4()),
            provider=self.provider.name,
            instruments=[],
            start=start,
            end=end,
            status="running",
            started_at=utcnow(),
        )
        self.register_universe(run.run_id)
        known = {i.instrument_id for i in self.store.instruments()}
        tickers = [t.upper() for t in (tickers or sorted(known))]
        run.instruments = tickers
        self.store.save_run(run)
        self.store.store_sessions(self.calendar.session_table(start, end))
        origins: set[str] = set()
        for t in tickers:
            say = progress or (lambda _m: None)
            if t not in known:
                run.errors.append(f"{t}: not in the instrument universe")
                continue
            try:
                self._ingest_one(t, start, end, bases, include_actions, incremental, refresh, run, origins)
                say(f"{t}: ok")
            except (FinanceAgentError, ValueError) as exc:
                run.errors.append(f"{t}: {type(exc).__name__}: {exc}")
                self._issue(
                    DataQualityIssue(
                        check=QualityCheck.PROVIDER_FAILURE,
                        severity="ERROR",
                        instrument_id=t,
                        provider=self.provider.name,
                        detail=f"ingestion failed for {start}..{end}: {type(exc).__name__}: {exc}",
                    ),
                    run,
                )
                say(f"{t}: FAILED ({type(exc).__name__})")
        run.requests = self.ctx.http.request_count - req0
        run.data_origin = ",".join(sorted(origins)) or None
        run.status = (
            "failed" if run.errors and len(run.errors) >= len(tickers) else
            "completed_with_errors" if run.errors else "completed"
        )  # fmt: skip
        run.finished_at = utcnow()
        run.duration_seconds = round(time.perf_counter() - t0, 3)
        self.store.save_run(run)
        return run

    def _issue(self, issue: DataQualityIssue, run: MarketDataIngestionRun) -> None:
        issue.run_id = run.run_id
        if self.store.add_issue(issue):
            run.quality_issues += 1

    def _ingest_one(self, t, start, end, bases, include_actions, incremental, refresh, run, origins) -> None:
        provider = self.provider.name
        symbol = self.store.provider_symbol(t, provider, end) or t
        if include_actions:
            fa = self.provider.corporate_actions(symbol, start, end, refresh=refresh)
            run.cache_hits += fa.cache_hits
            for pa in fa.actions:
                action, problem = self.to_action(t, pa)
                if action is None:
                    self._issue(
                        DataQualityIssue(
                            check=QualityCheck.INVALID_CORPORATE_ACTION,
                            severity="ERROR",
                            instrument_id=t,
                            provider=provider,
                            detail=f"corporate action {pa.action_id} rejected (not stored): {problem}",
                        ),
                        run,
                    )
                    continue
                origins.add(action.data_origin)
                outcome = self.store.insert_action(action)
                run.corporate_actions += outcome != "unchanged"
        for basis in bases:
            fetch_start = self._fetch_start(t, provider, str(basis), start, incremental and not refresh)
            fb = self.provider.daily_bars(symbol, fetch_start, end, basis, refresh=refresh)
            run.cache_hits += fb.cache_hits
            normalized: list[PriceBar] = []
            for pb in fb.bars:
                bar, issues = self.normalize(t, pb, basis)
                for i in issues:
                    self._issue(i, run)
                if bar is not None and fetch_start <= bar.session_date <= end:
                    normalized.append(bar)
            run.rows_received += len(normalized)
            normalized, dup_issues = duplicates(normalized)
            run.duplicates += sum(1 for i in dup_issues)
            for i in dup_issues:
                self._issue(i, run)
            for bar in normalized:
                origins.add(bar.data_origin)
                outcome = self.store.insert_bar(bar)
                if outcome == "unchanged":
                    run.unchanged += 1
                else:
                    run.versions_inserted += 1
                if outcome == "correction":
                    run.corrections += 1
                    self._issue(
                        DataQualityIssue(
                            check=QualityCheck.CORRECTION,
                            severity="INFO",
                            instrument_id=t,
                            provider=provider,
                            session_date=bar.session_date,
                            detail=f"provider changed a finalized {basis} bar; new version visible from "
                            f"{bar.retrieved_at.isoformat()} (earlier version retained)",
                        ),
                        run,
                    )
            self._series_checks(t, basis, start, end, run)

    def _fetch_start(self, t: str, provider: str, basis: str, start: date, incremental: bool) -> date:
        """Incremental: re-check the last few stored FINAL sessions *of this basis* so corrections
        and finalizations are captured; earlier sessions are not re-downloaded."""
        last = self.store.last_final_session(t, provider, basis) if incremental else None
        if last is None or last < start:
            return start
        recheck = last
        for _ in range(INCREMENTAL_RECHECK_SESSIONS):
            recheck = self.calendar.previous_session(recheck)
        return max(start, recheck)

    def _series_checks(self, t: str, basis: AdjustmentBasis, start: date, end: date, run) -> None:
        provider = self.provider.name
        visible = self.store.bars(t, adjustment=str(basis), start=start, end=end, provider=provider)
        status = self.store.session_status(t, str(basis), start, end)
        if not status:
            return
        first, last = min(status), max(status)  # gaps only inside the observed span
        expected = self.calendar.sessions(first, last)
        gaps = classify_gaps(expected, visible, status, failed=False)
        for i in gap_issues(t, provider, gaps, run.run_id):
            self._issue(i, run)
        actions = self.store.actions(t)
        for i in series_checks(t, provider, basis, expected, visible, actions, self.cfg, run.run_id):
            self._issue(i, run)
