"""Event-study orchestration: single-event (equity/ETF or Treasury yield) and bounded multi-event runs.

Every run is persisted immutably (see ``store``). Synthetic inputs propagate to the run's
``data_origin``/``synthetic`` flags, the report banner, the API response and the log record.
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from importlib import metadata

from finance_agent import __version__
from finance_agent.errors import NotFoundError
from finance_agent.event_studies import aggregation as agg
from finance_agent.event_studies import benchmarks, reports
from finance_agent.event_studies.alignment import align_event, event_time_for
from finance_agent.event_studies.confounders import detect
from finance_agent.event_studies.engine import SCOPE, apply_confounders, origin_of, run_equity_study
from finance_agent.event_studies.schemas import (
    METHODOLOGY_VERSION,
    DataMode,
    EstimationWindow,
    EventStudyRequest,
    EventStudyRun,
    EventStudySpecification,
    EventWindow,
    ExpectedReturnModel,
    ReturnType,
    StudyStatus,
    parse_model,
    sha256_json,
)
from finance_agent.event_studies.store import EventStudyStore
from finance_agent.event_studies.windows import grid_bounds, session_grid
from finance_agent.event_studies.yields_study import run_yield_study, yield_day0
from finance_agent.events.schemas import EventCategory, FinancialEvent, RevisionStatus
from finance_agent.events.store import EventStore
from finance_agent.market.calendar import get_calendar
from finance_agent.market.service import MarketDataService
from finance_agent.market.store import MarketStore
from finance_agent.timeutil import utcnow

log = logging.getLogger(__name__)

MAX_EVENTS_HARD_CAP = 100
ROBUST_ESTIMATION = (
    ("estimation [-250,-21] min 150", EstimationWindow(start=-250, end=-21, min_observations=150)),
    ("estimation [-120,-11] min 80", EstimationWindow(start=-120, end=-11, min_observations=80)),
    ("small sample [-60,-11] min 40", EstimationWindow(start=-60, end=-11, min_observations=40)),
)


def software_versions() -> dict:
    out = {"finance_agent": __version__, "methodology": METHODOLOGY_VERSION}
    for pkg in ("numpy", "scipy", "statsmodels", "exchange-calendars"):
        try:
            out[pkg] = metadata.version(pkg)
        except metadata.PackageNotFoundError:
            out[pkg] = None
    return out


def event_summary(e: FinancialEvent) -> dict:
    t, d, basis = event_time_for(e)
    return {
        "event_id": e.event_id,
        "title": e.title,
        "event_type": str(e.event_type),
        "event_subtype": str(e.event_subtype),
        "event_status": str(e.event_status),
        "revision_status": str(e.revision_status),
        "source_name": e.source_name,
        "source_url": e.source_url,
        "source_document_id": e.source_document_id,
        "source_snapshot_hash": e.source_snapshot_hash,
        "published_at": e.published_at.isoformat() if e.published_at else None,
        "available_at": e.available_at.isoformat() if e.available_at else None,
        "availability_precision": str(e.availability_precision),
        "time_used": t.isoformat() if t else (str(d) if d else None),
        "time_basis": str(basis),
        "importance_score": e.importance_score,
        "affected_entities": list(e.affected_entities),
        "data_origin": e.data_origin,
    }


def _event_core(e: FinancialEvent) -> dict:
    """Fields that change a study's inputs (immutable observed facts)."""
    return {k: v for k, v in event_summary(e).items() if k not in ("importance_score", "title", "source_url")}


def run_id_for(kind: str, spec_hash: str, data_digest: str) -> str:
    return "ES-" + sha256_json([kind, spec_hash, data_digest, METHODOLOGY_VERSION])[:16]


class EventStudyService:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.market = MarketDataService(ctx.db)
        self.mstore = MarketStore(ctx.db)
        self.events = EventStore(ctx.db)
        self.store = EventStudyStore(ctx.db)
        self.calendar = get_calendar()

    # ------------------------------------------------------------------ helpers
    def _event(self, event_id: str) -> FinancialEvent:
        e = self.events.get(event_id)
        if e is None:
            raise NotFoundError(f"event {event_id} not found (run `events ingest` first)")
        if not e.has_occurred:
            raise ValueError(f"{event_id} is a {e.event_status} calendar entry, not an occurred event")
        return e

    def spec_for(self, req: EventStudyRequest, e: FinancialEvent) -> EventStudySpecification:
        t, _d, basis = event_time_for(e)
        model = parse_model(req.model)
        is_yield = bool(req.series_id)
        windows = tuple(EventWindow.parse(w) for w in req.windows) if req.windows else None
        kw = {"event_windows": windows} if windows else {}
        return EventStudySpecification(
            kind="yield" if is_yield else "equity",
            event_id=e.event_id,
            event_source=e.source_name,
            event_published_at=t,
            event_time_basis=basis,
            target=(req.series_id or req.ticker).upper(),
            benchmark=None if is_yield or (model == ExpectedReturnModel.RAW and not req.benchmark)
            else benchmarks.definition(req.benchmark or benchmarks.DEFAULT_BENCHMARK),
            price_basis=None if is_yield else req.price_basis,
            return_type=req.return_type,
            market_data_source="market_yield_observations (ALFRED H.15)" if is_yield
            else "market_price_bars (v0.3-A store)",
            alignment_convention="yield-close-v1" if is_yield else "daily-close-v1",
            estimation_window=EstimationWindow(
                start=req.estimation_start, end=req.estimation_end, min_observations=req.min_observations
            ),
            model=ExpectedReturnModel.RAW if is_yield else model,
            test_method="descriptive (pre-event reference distribution)" if is_yield
            else "classical-t (MacKinlay 1997) + pre-event placebo",
            data_mode=req.data_mode,
            as_of=req.as_of if req.data_mode == DataMode.POINT_IN_TIME else None,
            **kw,
        )  # fmt: skip

    def _equity(self, spec: EventStudySpecification, e: FinancialEvent, alignment=None):
        alignment = alignment or align_event(e, self.calendar)
        res = run_equity_study(
            spec, alignment, market=self.market, calendar=self.calendar,
            event_available_at=e.available_at, event_origin=e.data_origin,
        )  # fmt: skip
        conf = None
        if alignment.day0 is not None and res.status not in (StudyStatus.INELIGIBLE,):
            lo = min(w.start for w in spec.event_windows)
            hi = max(w.end for w in spec.event_windows)
            try:
                grid = session_grid(self.calendar, alignment.day0, lo, hi)
                conf = detect(
                    self.events, self.calendar, e, alignment, grid, lo, hi, target=spec.target,
                    is_equity=True, as_of=spec.as_of,
                )  # fmt: skip
                apply_confounders(res, conf["confounders"])
            except Exception as exc:
                conf = {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
        res.confounders = conf
        return res

    # ------------------------------------------------------------------ single event
    def run(self, req: EventStudyRequest) -> EventStudyRun:
        e = self._event(req.event_id)
        spec = self.spec_for(req, e)
        if spec.kind == "yield":
            return self._run_yield(spec, e)
        res = self._equity(spec, e)
        payload = res.model_dump(mode="json")
        robustness = []
        if req.robustness and res.status in (StudyStatus.COMPLETED, StudyStatus.UNVERIFIED):
            robustness = self.robustness(spec, e, res)
        payload["robustness"] = robustness
        payload["event"] = event_summary(e)
        payload["reproduce"] = (
            f"uv run python -m finance_agent study run --event-id {e.event_id} --ticker {spec.target} "
            f"--benchmark {spec.benchmark.instrument_id if spec.benchmark else 'none'} "
            f"--model {spec.model} --basis {spec.price_basis} --mode {spec.data_mode}"
            + (f" --as-of {spec.as_of.isoformat()}" if spec.as_of else "")
        )
        digest = sha256_json(
            {
                "target": (res.data_manifest.get("target") or {}).get("bars_digest"),
                "benchmark": (res.data_manifest.get("benchmark") or {}).get("bars_digest"),
                "factors": [(m or {}).get("adjustment_factors") for m in res.data_manifest.values()],
                "event": _event_core(e),
                "confounders": sorted(c["event_id"] for c in (res.confounders or {}).get("confounders", [])),
                # robustness output is part of the deliverable: a run with checks is not the same run
                "robustness_requested": bool(req.robustness),
            }
        )
        rows = self._rows(e.event_id, spec, payload, robustness)
        return self._finalize("single", spec.model_dump(mode="json"), spec.spec_hash(), digest,
                              [e.event_id], res.status, res.data_origin, res.synthetic, payload,
                              res.data_manifest, rows)  # fmt: skip

    def robustness(self, spec: EventStudySpecification, e: FinancialEvent, primary) -> list[dict]:
        """Predefined sensitivity specifications. All are reported, including failures."""
        variants: list[tuple[str, EventStudySpecification]] = []
        for m in ExpectedReturnModel:
            if m != spec.model:
                variants.append((f"model: {m}", spec.model_copy(update={"model": m})))
        for b in benchmarks.alternatives(
            spec.target, spec.benchmark.instrument_id if spec.benchmark else None
        ):
            if spec.model != ExpectedReturnModel.RAW:
                variants.append(
                    (f"benchmark: {b}", spec.model_copy(update={"benchmark": benchmarks.definition(b)}))
                )
        for label, ew in ROBUST_ESTIMATION:
            if ew.end < min(w.start for w in spec.event_windows):
                variants.append((label, spec.model_copy(update={"estimation_window": ew})))
        if spec.return_type == ReturnType.SIMPLE:
            variants.append(("log returns", spec.model_copy(update={"return_type": ReturnType.LOG})))
        outl = [
            date.fromisoformat(o["session"])
            for o in (primary.estimation.outliers if primary.estimation else [])
        ]
        if outl:
            variants.append(
                (f"excluding {len(outl)} estimation outlier(s)",
                 spec.model_copy(update={"excluded_estimation_sessions": tuple(outl)}))
            )  # fmt: skip
        alignment = align_event(e, self.calendar)
        out = []
        for label, v in variants:
            try:
                r = run_equity_study(
                    v, alignment, market=self.market, calendar=self.calendar,
                    event_available_at=e.available_at, event_origin=e.data_origin,
                )  # fmt: skip
                out.append(
                    {
                        "variant": label, "spec_hash": v.spec_hash(), "status": str(r.status),
                        "reason": r.status_reason,
                        "beta": r.estimation.beta if r.estimation else None,
                        "estimation_observations": r.estimation.valid_observations if r.estimation else None,
                        "windows": {
                            w.window: {
                                "status": str(w.status),
                                "car": w.car if w.car is not None else (w.raw_cumulative_return if v.model == ExpectedReturnModel.RAW else None),
                                "p_value": w.inference.p_value if w.inference else None,
                                "ci": [w.inference.ci_low, w.inference.ci_high] if w.inference else None,
                                "inference_status": str(w.inference.status) if w.inference else None,
                            }
                            for w in r.windows
                        },
                        "measure": "raw cumulative return" if v.model == ExpectedReturnModel.RAW else "CAR",
                    }
                )  # fmt: skip
            except Exception as exc:
                out.append({"variant": label, "spec_hash": v.spec_hash(), "status": "FAILED",
                            "reason": f"{type(exc).__name__}: {exc}", "windows": {}})  # fmt: skip
        return out

    # ------------------------------------------------------------------ yields
    def _run_yield(self, spec: EventStudySpecification, e: FinancialEvent) -> EventStudyRun:
        alignment = align_event(e, self.calendar)
        res = run_yield_study(spec, alignment, self.mstore, event_available_at=e.available_at,
                              event_origin=e.data_origin)  # fmt: skip
        if res.get("status") == StudyStatus.COMPLETED:
            rows_y = self.mstore.yields(spec.target, as_of=spec.as_of)
            obs_days = [r["observation_date"] for r in rows_y if r["value"] is not None]
            day0 = date.fromisoformat(res["day0"])
            i0 = obs_days.index(day0)
            lo = min(w.start for w in spec.event_windows)
            hi = min(max(w.end for w in spec.event_windows), len(obs_days) - 1 - i0)
            grid = {k: obs_days[i0 + k] for k in range(lo, hi + 1)}
            res["confounders"] = detect(
                self.events, self.calendar, e, alignment, grid, lo, hi, target=spec.target,
                is_equity=False, as_of=spec.as_of, day0_fn=lambda ev, al: yield_day0(al, obs_days)[0],
            )  # fmt: skip
        res["event"] = event_summary(e)
        res["reproduce"] = (
            f"uv run python -m finance_agent study run --event-id {e.event_id} --series {spec.target} "
            f"--mode {spec.data_mode}" + (f" --as-of {spec.as_of.isoformat()}" if spec.as_of else "")
        )
        payload = _json(res)
        digest = sha256_json(
            {"levels": [(d["date"], d["yield_pct"]) for d in payload.get("daily_changes", [])],
             "vintages": (payload.get("data_manifest") or {}).get("vintages"), "event": _event_core(e),
             "confounders": sorted(c["event_id"] for c in (payload.get("confounders") or {}).get("confounders", []))}
        )  # fmt: skip
        rows = [
            {
                "event_id": e.event_id,
                "target": spec.target,
                "variant": "primary",
                "model": "yield_change",
                "window": w["window"],
                "window_start": w["start"],
                "window_end": w["end"],
                "status": str(w["status"]),
                "measure": "change_bp",
                "unit": "basis_points",
                "value": w.get("change_bp"),
                "standard_error": None,
                "test_statistic": None,
                "p_value": None,
                "ci_low": None,
                "ci_high": None,
                "inference_status": "NOT_APPLICABLE",
                "estimation_observations": None,
            }
            for w in payload.get("windows", [])
        ]
        return self._finalize("yield", spec.model_dump(mode="json"), spec.spec_hash(), digest, [e.event_id],
                              StudyStatus(payload["status"]), payload.get("data_origin", "unknown"),
                              bool(payload.get("synthetic")), payload, payload.get("data_manifest", {}), rows)  # fmt: skip

    # ------------------------------------------------------------------ multi-event
    def compare(
        self,
        *,
        category: str,
        ticker: str,
        benchmark: str = "SPY",
        subtypes: list[str] | None = None,
        model: str = "market-model",
        price_basis: str = "total_return",
        start: datetime | None = None,
        end: datetime | None = None,
        windows: list[str] | None = None,
        data_mode: DataMode = DataMode.RESEARCH,
        as_of: datetime | None = None,
        max_events: int = 40,
        first_release_only: bool = True,
        exclude_confounder_types: list[str] | None = None,
        time_budget_s: float | None = None,
    ) -> EventStudyRun:
        cat = EventCategory(category)
        max_events = max(1, min(int(max_events), MAX_EVENTS_HARD_CAP))
        budget = (
            time_budget_s if time_budget_s is not None else self.ctx.settings.event_study_time_budget_seconds
        )
        excl = exclude_confounder_types or [str(EventCategory.MACRO_RELEASE), str(EventCategory.CENTRAL_BANK),
                                            str(EventCategory.CORPORATE_EARNINGS)]  # fmt: skip
        m = parse_model(model)
        ev_windows = tuple(EventWindow.parse(w) for w in windows) if windows else None
        pit = data_mode == DataMode.POINT_IN_TIME
        cands = self.events.query(categories=[str(cat)], subtypes=subtypes, start=start, end=end,
                                  as_of=as_of if pit else None, limit=5000)  # fmt: skip
        cands = [e for e in cands if e.has_occurred]
        if first_release_only:
            cands = [e for e in cands if e.revision_status != RevisionStatus.REVISION]
        if cat not in (EventCategory.MACRO_RELEASE, EventCategory.CENTRAL_BANK):
            cands = [e for e in cands if ticker.upper() in e.affected_entities]
        cands.sort(key=lambda e: (e.available_at, e.event_id))
        truncated = max(0, len(cands) - max_events)
        cands = cands[:max_events]
        selection = {
            "category": str(cat), "subtypes": subtypes, "target": ticker.upper(), "benchmark": benchmark.upper(),
            "model": str(m), "price_basis": price_basis, "data_mode": str(data_mode),
            "as_of": as_of.isoformat() if as_of else None, "start": start.isoformat() if start else None,
            "end": end.isoformat() if end else None, "first_release_only": first_release_only,
            "max_events": max_events, "events_selected": len(cands), "events_truncated_by_cap": truncated,
            "selection_rule": "all occurred events matching the filter, ordered by availability; "
            "no outcome-based selection",
        }  # fmt: skip
        t0 = time.monotonic()
        items, excluded, origins, per_hash, skipped = [], [], [], [], []
        alt_model = (ExpectedReturnModel.MARKET_ADJUSTED if m == ExpectedReturnModel.MARKET_MODEL
                     else ExpectedReturnModel.MARKET_MODEL)  # fmt: skip
        alt_items = []
        for e in cands:
            if time.monotonic() - t0 > budget:
                skipped.append(e.event_id)
                continue
            req = EventStudyRequest(
                event_id=e.event_id, ticker=ticker, benchmark=benchmark, model=str(m), price_basis=price_basis,
                data_mode=data_mode, as_of=as_of if pit else None, robustness=False, windows=windows,
            )  # fmt: skip
            spec = self.spec_for(req, e).model_copy(
                update={"event_category_filter": f"{cat}" + (f":{','.join(subtypes)}" if subtypes else "")}
            )
            alignment = align_event(e, self.calendar)
            res = self._equity(spec, e, alignment)
            origins.append(res.data_origin)
            per_hash.append(spec.spec_hash())
            entry = {
                "event_id": e.event_id, "event_subtype": str(e.event_subtype), "event_type": str(e.event_type),
                "source_name": e.source_name, "status": str(res.status), "reason": res.status_reason,
                "alignment": res.alignment.model_dump(mode="json") if res.alignment else {},
                "day0": str(res.alignment.day0) if res.alignment and res.alignment.day0 else None,
                "windows": [w.model_dump(mode="json") for w in res.windows],
                "confounders": (res.confounders or {}).get("confounders", []),
                "confounder_status": (res.confounders or {}).get("status"),
                "confounder_count": len((res.confounders or {}).get("confounders", [])),
                "bars_digest": [(res.data_manifest.get(k) or {}).get("bars_digest") for k in ("target", "benchmark")],
            }  # fmt: skip
            if res.status in (StudyStatus.COMPLETED, StudyStatus.UNVERIFIED):
                entry["session_span"] = agg.span_dates(entry["windows"])
                items.append(entry)
                if m != ExpectedReturnModel.RAW:
                    alt = run_equity_study(
                        spec.model_copy(update={"model": alt_model}), alignment, market=self.market,
                        calendar=self.calendar, event_available_at=e.available_at, event_origin=e.data_origin,
                    )  # fmt: skip
                    if alt.status in (StudyStatus.COMPLETED, StudyStatus.UNVERIFIED):
                        alt_items.append(
                            {**entry, "windows": [w.model_dump(mode="json") for w in alt.windows]}
                        )
            else:
                excluded.append(
                    {"event_id": e.event_id, "status": str(res.status), "reason": res.status_reason}
                )
        labels = [
            w.label for w in (ev_windows or EventStudySpecification.model_fields["event_windows"].default)
        ]
        clusters = agg.build_clusters(items, (0, 0))
        cluster_of = {
            eid: c["cluster_id"] for c in clusters for a in c["announcements"] for eid in a["event_ids"]
        }
        for it in items:
            it["cluster_id"] = cluster_of.get(it["event_id"])
        restricted = [
            c for c in clusters
            if not any(x["event_type"] in excl for r in c["results"] for x in r["confounders"])
        ]  # fmt: skip
        primary = agg.aggregate(clusters, labels)
        alt_clusters = agg.build_clusters(alt_items, (0, 0))
        sensitivity = {
            "restricted_sample_excluding_confounded": {
                "excluded_confounder_types": excl,
                "clusters_kept": len(restricted),
                "clusters_dropped": len(clusters) - len(restricted),
                "aggregate": agg.aggregate(restricted, labels),
            },
            f"alternative model: {alt_model}": {"aggregate": agg.aggregate(alt_clusters, labels)}
            if m != ExpectedReturnModel.RAW else {"status": "NOT_APPLICABLE"},
            "difference_full_minus_restricted_mean_car": {
                lb: (primary[lb]["car"].get("mean") - agg.aggregate(restricted, labels)[lb]["car"].get("mean"))
                if primary[lb]["car"].get("mean") is not None and agg.aggregate(restricted, labels)[lb]["car"].get("mean") is not None
                else None
                for lb in labels
            },
        }  # fmt: skip
        origin, synth = origin_of(origins + [e.data_origin for e in cands])
        status = StudyStatus.COMPLETED if clusters else StudyStatus.INSUFFICIENT_DATA
        if any(it["status"] == StudyStatus.UNVERIFIED for it in items):
            status = StudyStatus.UNVERIFIED
        if skipped:
            status = StudyStatus.PARTIAL
        selection.update(
            events_studied=len(items) + len(excluded), events_eligible=len(items), events_excluded=len(excluded),
            events_skipped_time_budget=len(skipped), announcements=sum(len(c["announcements"]) for c in clusters),
            independent_clusters=len(clusters),
            overlapping_clusters=sum(1 for c in clusters if c["overlapping"]),
            missing_data_rate=(len(excluded) / (len(items) + len(excluded))) if (items or excluded) else None,
        )  # fmt: skip
        spec_doc = {
            "kind": "compare", **selection, "per_event_spec_hashes": per_hash,
            "exclude_confounder_types": excl, "methodology_version": METHODOLOGY_VERSION,
            "target": ticker.upper(),
        }  # fmt: skip
        for k in ("events_studied", "events_eligible", "events_excluded", "events_skipped_time_budget",
                  "announcements", "independent_clusters", "overlapping_clusters", "missing_data_rate"):  # fmt: skip
            spec_doc.pop(k, None)
        spec_hash = sha256_json(spec_doc)
        payload = {
            "selection": selection,
            "events": [{k: v for k, v in it.items() if k not in ("session_span",)} for it in items] + excluded,
            "excluded": excluded,
            "skipped_time_budget": skipped,
            "clusters_summary": [
                {"cluster_id": c["cluster_id"], "overlapping": c["overlapping"], "announcements": c["announcements"]}
                for c in clusters
            ],
            "aggregate": primary,
            "sensitivity": sensitivity,
            "interpretation_scope": SCOPE,
            "limitations": [
                "one target per comparison: several assets on the same announcement are never pooled as independent",
                "events released together count once; overlapping windows are merged into one cluster",
                "cross-sectional tests (LIMITED) assume independent clusters and ignore estimation-window overlap",
                "the primary specification was fixed before computing; restricted-sample and alternative-model "
                "results are sensitivity checks, not alternatives to choose from",
                "no clustered/panel inference for shared market shocks across assets is implemented",
            ],
            "reproduce": f"uv run python -m finance_agent study compare --event-type {cat} --ticker {ticker.upper()} "
            f"--benchmark {benchmark.upper()} --model {m}" + (f" --subtype {' --subtype '.join(subtypes)}" if subtypes else ""),
        }  # fmt: skip
        payload = _json(payload)
        digest = sha256_json({"events": [(it["event_id"], it["bars_digest"], [c["event_id"] for c in it["confounders"]])
                                         for it in items], "excluded": excluded})  # fmt: skip
        rows = []
        for it in items:
            for w in it["windows"]:
                rows.append(self._row(it["event_id"], ticker.upper(), "primary", str(m), w))
        for lb, a in primary.items():
            win = EventWindow.parse(lb)
            t = a["cross_sectional_test"]
            rows.append({
                "event_id": "AGGREGATE", "target": ticker.upper(), "variant": "primary", "model": str(m), "window": lb,
                "window_start": win.start, "window_end": win.end, "status": "AGGREGATE", "measure": "mean_cluster_car",
                "unit": "decimal_return", "value": a["car"].get("mean"), "standard_error": t.get("standard_error"),
                "test_statistic": t.get("test_statistic"), "p_value": t.get("p_value"), "ci_low": t.get("ci_low"),
                "ci_high": t.get("ci_high"), "inference_status": t.get("status"),
                "estimation_observations": a["clusters_with_valid_window"],
            })  # fmt: skip
        return self._finalize("compare", _json(spec_doc), spec_hash, digest, [it["event_id"] for it in items],
                              status, origin, synth, payload, {"per_event": [it["bars_digest"] for it in items]},
                              rows)  # fmt: skip

    # ------------------------------------------------------------------ persistence
    def _row(self, event_id: str, target: str, variant: str, model: str, w: dict) -> dict:
        inf = w.get("inference") or {}
        is_raw = model == ExpectedReturnModel.RAW
        return {
            "event_id": event_id, "target": target, "variant": variant[:64], "model": model, "window": w["window"],
            "window_start": w["start"], "window_end": w["end"], "status": str(w["status"]),
            "measure": "raw_cumulative_return" if is_raw else "car", "unit": "decimal_return",
            "value": w.get("raw_cumulative_return") if is_raw else w.get("car"),
            "standard_error": inf.get("standard_error"), "test_statistic": inf.get("test_statistic"),
            "p_value": inf.get("p_value"), "ci_low": inf.get("ci_low"), "ci_high": inf.get("ci_high"),
            "inference_status": inf.get("status"), "estimation_observations": None,
        }  # fmt: skip

    def _rows(
        self, event_id: str, spec: EventStudySpecification, payload: dict, robustness: list[dict]
    ) -> list[dict]:
        n = (payload.get("estimation") or {}).get("valid_observations")
        rows = []
        for w in payload.get("windows", []):
            r = self._row(event_id, spec.target, "primary", str(spec.model), w)
            r["estimation_observations"] = n
            rows.append(r)
        for v in robustness:
            for lb, w in v.get("windows", {}).items():
                win = EventWindow.parse(lb)
                rows.append({
                    "event_id": event_id, "target": spec.target, "variant": v["variant"][:64],
                    "model": str(spec.model), "window": lb, "window_start": win.start, "window_end": win.end,
                    "status": w["status"], "measure": "car" if v.get("measure") == "CAR" else "raw_cumulative_return",
                    "unit": "decimal_return", "value": w.get("car"), "standard_error": None, "test_statistic": None,
                    "p_value": w.get("p_value"), "ci_low": (w.get("ci") or [None, None])[0],
                    "ci_high": (w.get("ci") or [None, None])[1], "inference_status": w.get("inference_status"),
                    "estimation_observations": v.get("estimation_observations"),
                })  # fmt: skip
        return rows

    def _finalize(self, kind, spec_doc, spec_hash, digest, event_ids, status, origin, synth, payload,
                  manifest, rows) -> EventStudyRun:  # fmt: skip
        run_id = run_id_for(kind, spec_hash, digest)
        existing = self.store.get(run_id)
        if existing is not None:
            existing.existing = True
            return existing
        run = EventStudyRun(
            run_id=run_id, kind=kind, status=status, spec_hash=spec_hash, data_digest=digest,
            specification=_json(spec_doc), event_ids=event_ids, data_origin=origin, synthetic=synth,
            created_at=utcnow(), software=software_versions(), result=payload,
        )  # fmt: skip
        _, md = reports.build(run.model_dump(mode="json"))
        run.report_markdown = md
        saved = self.store.save(run, rows, _json(manifest))
        log.info(
            "event study finalized",
            extra={
                "run_id": run_id,
                "kind": kind,
                "status": str(status),
                "data_origin": origin,
                "synthetic": synth,
            },
        )
        return saved

    def get(self, run_id: str) -> EventStudyRun:
        r = self.store.get(run_id)
        if r is None:
            raise NotFoundError(f"event-study run {run_id} not found")
        return r

    def report(self, run_id: str) -> dict:
        r = self.get(run_id)
        sections, md = reports.build(r.model_dump(mode="json"))
        return {
            "run_id": r.run_id, "data_origin": r.data_origin, "synthetic": r.synthetic,
            "banner": reports.SYNTHETIC_BANNER if r.synthetic else reports.REAL_BANNER,
            "sections": _json(sections), "markdown": r.report_markdown or md,
        }  # fmt: skip


def _json(obj):
    """JSON-safe copy (dates/datetimes/enums -> strings) for persistence."""
    import json

    return json.loads(json.dumps(obj, default=str))


__all__ = ["EventStudyService", "grid_bounds", "software_versions"]
