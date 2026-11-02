"""v0.3-B event studies end to end on the offline database.

Events are REAL recorded v0.2 snapshots (plus SYNTHETIC events constructed here and labelled
``fixture:synthetic``); equity prices are SYNTHETIC Alpaca-format fixtures; Treasury yields are REAL
ALFRED vintages. Equity results are therefore methodological validation only. Independent
references are computed straight from the fixture files (JSON bars, ALFRED CSV) with numpy,
not through the engine.
"""

from __future__ import annotations

import csv
import json
import re
from datetime import UTC, datetime

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

from finance_agent.event_studies.schemas import EventStudyRequest
from finance_agent.event_studies.service import EventStudyService
from finance_agent.events.schemas import (
    AvailabilityPrecision,
    EventCategory,
    EventStatus,
    EventSubtype,
    FinancialEvent,
    VerificationStatus,
)
from finance_agent.events.store import EventStore
from finance_agent.timeutil import ET
from tests.conftest import FIXTURES

FOMC_SEP25 = "EV-e6a9f7badcb72f25"  # REAL: FOMC rate cut, 2025-09-17 14:00 ET
CPI_OCT26 = "EV-eeda2fff13dd7634"  # REAL: CPI, 2026-10-14 08:30 ET (scheduled time, confirmed date)
MSFT_EARN = "EV-41f767086e2b42a4"  # REAL: MSFT earnings 8-K, 2026-10-28 16:04:44 ET
GDP_DATEONLY = "EV-2911ad2941617fa4"  # REAL: real GDP release known by date only


def ny(*a) -> datetime:
    return datetime(*a, tzinfo=ET)


def add_event(ctx, key, when, entities, *, category=EventCategory.OTHER_DISCLOSURE,
              subtype=EventSubtype.OTHER_MATERIAL_EVENT, available=None, source="SYNTHETIC test source"):  # fmt: skip
    ev = FinancialEvent(
        canonical_event_key=f"synthetic:{key}", event_type=category, event_subtype=subtype,
        event_status=EventStatus.PUBLISHED, title=f"SYNTHETIC {key}", source_name=source,
        data_origin="fixture:synthetic", affected_entities=entities, published_at=when,
        available_at=available or when, availability_precision=AvailabilityPrecision.EXACT_TIMESTAMP,
        verification_status=VerificationStatus.UNVERIFIED,
    )  # fmt: skip
    EventStore(ctx.db).upsert(ev)
    return ev


def run(ctx, event_id, **kw):
    kw.setdefault("price_basis", "total_return")
    kw.setdefault("robustness", False)
    return EventStudyService(ctx).run(EventStudyRequest(event_id=event_id, **kw))


def win(r, label):
    return next(w for w in r.result["windows"] if w["window"] == label)


# ------------------------------------------------------------------ single event
def test_fomc_study_end_to_end_is_labelled_synthetic(study_ctx):
    r = run(study_ctx, FOMC_SEP25, ticker="QQQ", benchmark="SPY", robustness=True)
    assert r.status == "COMPLETED" and r.synthetic and r.data_origin == "fixture:synthetic"
    al = r.result["alignment"]
    assert al["timing_class"] == "REGULAR_SESSION" and al["day0"] == "2025-09-17"
    assert "pre-announcement contamination" in al["contamination_warning"]
    est = r.result["estimation"]
    assert (
        est["expected_sessions"] == 130 and est["valid_observations"] == 130 and est["missing_sessions"] == []
    )
    assert {w["window"] for w in r.result["windows"]} == {"[0,0]", "[0,+1]", "[-1,+1]", "[0,+5]"}
    assert all(w["status"] == "VALID" for w in r.result["windows"])
    # the day-0 window is flagged because the release came during trading hours
    assert "day-0 return includes pre-release trading" in win(r, "[0,0]")["inference"]["warnings"]
    assert r.result["interpretation_scope"]["D_causal_identification"].startswith("NOT ESTABLISHED")
    variants = [v["variant"] for v in r.result["robustness"]]
    assert (
        "model: market_adjusted" in variants
        and "benchmark: QQQ" not in variants
        and "log returns" in variants
    )
    assert any(v.startswith("estimation [-250") for v in variants)
    assert "SYNTHETIC DATA - METHODOLOGICAL VALIDATION ONLY" in r.report_markdown
    for title in ("1. Research question", "11. Confounding-event audit", "14. Reproducibility manifest"):
        assert f"## {title}" in r.report_markdown
    m = r.result["data_manifest"]["target"]
    assert m["query_mode"] == "research" and m["retrospectively_adjusted"] is True and m["bars_digest"]


def _fixture_closes(sym: str) -> dict[str, float]:
    raw = json.loads((FIXTURES / "alpaca" / "bars" / "raw" / f"{sym}.json").read_text(encoding="utf-8"))
    return {b["t"][:10]: b["c"] for b in raw["bars"][sym]}


def test_car_matches_independent_reference_on_the_injected_xle_move(study_ctx):
    """The SYNTHETIC fixtures inject +30 % into XLE on 2025-08-14. Reference: numpy lstsq on raw closes."""
    ev = add_event(study_ctx, "xle-move", ny(2025, 8, 14, 8, 30), ["XLE"])
    r = run(study_ctx, ev.event_id, ticker="XLE", benchmark="SPY", price_basis="raw")
    assert r.status == "COMPLETED" and r.result["alignment"]["timing_class"] == "PRE_MARKET"
    xle, spy = _fixture_closes("XLE"), _fixture_closes("SPY")
    days = sorted(set(xle) & set(spy))
    i0 = days.index("2025-08-14")

    def ret(c, i):
        return c[days[i]] / c[days[i - 1]] - 1.0

    ri = np.array([ret(xle, i0 + k) for k in range(-150, -20)])
    rm = np.array([ret(spy, i0 + k) for k in range(-150, -20)])
    (a, b), *_ = np.linalg.lstsq(np.column_stack([np.ones_like(rm), rm]), ri, rcond=None)
    ar = {k: ret(xle, i0 + k) - (a + b * ret(spy, i0 + k)) for k in range(-1, 6)}
    est = r.result["estimation"]
    assert est["alpha"] == pytest.approx(a, abs=1e-12) and est["beta"] == pytest.approx(b, abs=1e-10)
    assert win(r, "[0,0]")["car"] == pytest.approx(ar[0], abs=1e-12)
    assert win(r, "[-1,+1]")["car"] == pytest.approx(ar[-1] + ar[0] + ar[1], abs=1e-12)
    assert win(r, "[0,+5]")["car"] == pytest.approx(sum(ar[k] for k in range(6)), abs=1e-12)
    assert ret(xle, i0) > 0.29 and win(r, "[0,0]")["inference"]["p_value"] < 1e-6
    # compounded asset return is reported separately from the (arithmetic) CAR
    w = win(r, "[0,+1]")
    assert w["raw_cumulative_return"] == pytest.approx(
        (1 + ret(xle, i0)) * (1 + ret(xle, i0 + 1)) - 1, abs=1e-12
    )


def test_market_adjusted_abnormal_returns_reconcile_with_returns(study_ctx):
    r = run(study_ctx, FOMC_SEP25, ticker="QQQ", benchmark="SPY", model="market-adjusted")
    for o in r.result["observations"]:
        assert o["abnormal_return"] == pytest.approx(o["asset_return"] - o["benchmark_return"], abs=1e-15)
    w = win(r, "[0,+5]")
    assert w["car"] == pytest.approx(
        sum(o["abnormal_return"] for o in r.result["observations"] if 0 <= o["relative_session"] <= 5),
        abs=1e-14,
    )
    assert w["inference"]["degrees_of_freedom"] == 129


def test_raw_basis_refuses_returns_across_the_nvda_split(study_ctx):
    ev = add_event(study_ctx, "nvda-pre-split", ny(2024, 6, 7, 8, 0), ["NVDA"])
    kw = dict(ticker="NVDA", benchmark="SPY", estimation_start=-100, estimation_end=-11, min_observations=60)
    raw = run(study_ctx, ev.event_id, price_basis="raw", **kw)
    w = win(raw, "[0,+5]")  # 2024-06-07 .. 2024-06-14 contains the 2024-06-10 ex-date
    assert w["status"] == "MISSING_OBSERVATIONS" and "split ex-date" in w["reason"] and w["car"] is None
    assert win(raw, "[0,0]")["status"] == "VALID"
    adj = run(study_ctx, ev.event_id, price_basis="split_adjusted", **kw)
    assert win(adj, "[0,+5]")["status"] == "VALID" and abs(win(adj, "[0,+5]")["raw_cumulative_return"]) < 0.5


def test_missing_benchmark_session_invalidates_window_and_is_never_filled(study_ctx):
    ev = add_event(study_ctx, "qqq-iwm", ny(2025, 3, 13, 8, 0), ["QQQ"])  # IWM has no bar on 2025-03-14
    r = run(study_ctx, ev.event_id, ticker="QQQ", benchmark="IWM")
    assert win(r, "[0,0]")["status"] == "VALID"
    for label in ("[0,+1]", "[-1,+1]", "[0,+5]"):
        w = win(r, label)
        assert (
            w["status"] == "MISSING_OBSERVATIONS"
            and "2025-03-14" in w["reason"]
            and "benchmark" in w["reason"]
        )
        assert (
            len(w["sessions"])
            == int(label.strip("[]").split(",")[1]) - int(label.strip("[]").split(",")[0]) + 1
        )
    o = next(o for o in r.result["observations"] if o["session"] == "2025-03-14")
    assert o["benchmark_return"] is None and o["abnormal_return"] is None
    # Regression (release audit): invalid windows rendered `| None |` as their inference status
    md = r.report_markdown
    inference_md = md[md.index("## 10.") : md.index("## 11.")]
    for label in ("[0,+1]", "[-1,+1]", "[0,+5]"):
        assert f"| {label} | not computed (window MISSING_OBSERVATIONS) |" in inference_md
    assert "| None" not in md and "None |" not in md


def test_after_hours_earnings_and_provisional_bar(study_ctx):
    r = run(study_ctx, MSFT_EARN, ticker="MSFT", benchmark="SPY")
    al = r.result["alignment"]
    assert al["timing_class"] == "AFTER_HOURS" and al["day0"] == "2026-10-29"
    assert win(r, "[0,0]")["status"] == "VALID"
    w = win(r, "[0,+1]")  # 2026-10-30 is PROVISIONAL in the fixtures: excluded, window invalid
    assert w["status"] == "MISSING_OBSERVATIONS" and "2026-10-30" in w["reason"]
    assert win(r, "[0,+5]")["status"] == "MISSING_OBSERVATIONS"


def test_date_only_release_makes_zero_window_ineligible(study_ctx):
    r = run(study_ctx, GDP_DATEONLY, ticker="SPY", benchmark="QQQ")
    assert r.result["alignment"]["timing_class"] == "UNCERTAIN_TIME"
    assert win(r, "[0,0]")["status"] == "INELIGIBLE_TIMING"
    assert win(r, "[0,+1]")["status"] == "VALID" and win(r, "[-1,+1]")["status"] == "VALID"


def test_confounders_same_announcement_and_other_company(study_ctx):
    cpi = run(study_ctx, CPI_OCT26, ticker="QQQ", benchmark="SPY")
    conf = cpi.result["confounders"]
    assert any(c["event_subtype"] == "CORE_CPI" for c in conf["same_announcement"])
    assert all(c["event_subtype"] != "CORE_CPI" for c in conf["confounders"])
    # injected market-wide release one session after a MSFT event; another company's filing is ignored
    base = add_event(study_ctx, "msft-base", ny(2025, 5, 6, 8, 0), ["MSFT"])
    add_event(study_ctx, "macro-confounder", ny(2025, 5, 7, 8, 30), ["SYN"], category=EventCategory.MACRO_RELEASE,
              subtype=EventSubtype.REAL_GDP, source="SYNTHETIC macro")  # fmt: skip
    add_event(study_ctx, "amzn-filing", ny(2025, 5, 7, 9, 0), ["AMZN"])
    r = run(study_ctx, base.event_id, ticker="MSFT", benchmark="SPY")
    c = r.result["confounders"]
    assert c["status"] == "CONFOUNDERS_PRESENT"
    assert [(x["event_subtype"], x["relative_session"]) for x in c["confounders"]] == [("REAL_GDP", 1)]
    assert "importance" in c["confounders"][0]["importance_note"]
    assert any("other recorded event" in x for x in win(r, "[0,+1]")["inference"]["warnings"])
    assert not any("other recorded event" in x for x in win(r, "[0,0]")["inference"]["warnings"])
    # May 2025 precedes the recorded macro/FOMC coverage: incompleteness is reported, not hidden
    assert c["coverage"]["macro (BLS/BEA via ALFRED)"]["status"] == "NOT_COVERED"


def _alfred(series: str) -> dict[str, float]:
    vintage = json.loads((FIXTURES / "MANIFEST.json").read_text(encoding="utf-8"))["yield_window"]["vintage"]
    path = FIXTURES / "alfred" / series / f"{vintage}.csv"
    with path.open(encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return {d: float(v) for d, v in rows[1:] if v not in (".", "")}


def test_yield_study_uses_basis_points_and_matches_alfred_csv(study_ctx):
    r = run(study_ctx, FOMC_SEP25, series_id="DGS10")
    res = r.result
    assert r.status == "COMPLETED" and not r.synthetic and r.data_origin == "fixture:real_snapshot"
    assert res["unit"] == "basis_points" and res["day0"] == "2025-09-17"  # 14:00 < ~15:30 ET quote time
    y = _alfred("DGS10")
    days = sorted(y)
    i0 = days.index("2025-09-17")
    for w in res["windows"]:
        ref = 100 * (y[days[i0 + w["end"]]] - y[days[i0 + w["start"] - 1]])
        assert w["change_bp"] == pytest.approx(ref, abs=1e-9)
        assert "car" not in w  # yields never pass through equity-return functions
    assert "SYNTHETIC DATA" not in r.report_markdown and "do not identify causal effects" in r.report_markdown
    late = run(study_ctx, MSFT_EARN, series_id="DGS2")  # 16:04 ET release -> next observation day
    assert late.result["day0"] == "2026-10-29"


def test_compare_counts_each_announcement_once(study_ctx):
    svc = EventStudyService(study_ctx)
    c = svc.compare(category="MACRO_RELEASE", subtypes=["CPI", "CORE_CPI"], ticker="QQQ", benchmark="SPY",
                    start=ny(2025, 7, 1))  # fmt: skip
    sel = c.result["selection"]
    assert c.synthetic and sel["events_eligible"] == 2 * sel["announcements"]
    assert sel["independent_clusters"] <= sel["announcements"]
    # independent recomputation of the primary aggregate from the per-event results
    by_cluster: dict[str, list[float]] = {}
    for e in c.result["events"]:
        if e.get("cluster_id"):
            w = next(w for w in e["windows"] if w["window"] == "[0,+1]")
            by_cluster.setdefault(e["cluster_id"], []).append(w["car"])
    ref = np.mean([np.mean(v) for v in by_cluster.values()])
    assert c.result["aggregate"]["[0,+1]"]["car"]["mean"] == pytest.approx(ref, abs=1e-14)
    assert c.result["aggregate"]["[0,+1]"]["car"]["n"] == len(by_cluster)
    t = c.result["aggregate"]["[0,+1]"]["cross_sectional_test"]
    assert t["status"] in ("LIMITED", "NOT_REPORTED") and t["status"] != "OK"
    sens = c.result["sensitivity"]
    assert "restricted_sample_excluding_confounded" in sens and any(
        k.startswith("alternative model") for k in sens
    )
    assert "SYNTHETIC DATA" in c.report_markdown


def test_runs_are_immutable_and_reproducible(study_ctx):
    a = run(study_ctx, FOMC_SEP25, ticker="QQQ", benchmark="SPY", windows=["0:1"])
    b = run(study_ctx, FOMC_SEP25, ticker="QQQ", benchmark="SPY", windows=["0:1"])
    assert b.existing and b.run_id == a.run_id and b.result == a.result
    svc = EventStudyService(study_ctx)
    rows = svc.store.results(a.run_id)
    assert len(rows) == 1 and rows[0]["measure"] == "car" and rows[0]["value"] == win(a, "[0,+1]")["car"]
    c = run(study_ctx, FOMC_SEP25, ticker="QQQ", benchmark="SPY", windows=["0:2"])
    assert c.run_id != a.run_id and svc.get(a.run_id).result == a.result
    assert a.software["statsmodels"] and a.methodology_version == "event-study-v0.3b.1"


# ------------------------------------------------------------------ API and CLI
def test_api_event_study_routes(study_ctx):
    from finance_agent.interfaces.api import create_app

    body = {"event_id": FOMC_SEP25, "ticker": "QQQ", "benchmark": "SPY", "price_basis": "total_return",
            "robustness": False}  # fmt: skip
    with TestClient(create_app(study_ctx.settings, study_ctx)) as cl:
        spec = cl.get("/event-studies/specifications").json()
        assert spec["defaults"]["estimation_window"] == {"start": -150, "end": -21, "min_observations": 100}
        assert cl.post("/event-studies", json=body).status_code == 403  # disabled without a token
        assert cl.get("/event-studies/not-a-run").status_code == 422
        assert cl.get("/event-studies/ES-0000000000000000").status_code == 404
    s = study_ctx.settings.model_copy(update={"ingest_admin_token": SecretStr("tok")})
    old, study_ctx.settings = study_ctx.settings, s
    try:
        with TestClient(create_app(s, study_ctx)) as cl:
            assert cl.post("/event-studies", json=body).status_code == 401
            h = {"X-Admin-Token": "tok"}
            assert cl.post("/event-studies", json={**body, "price_basis": None}, headers=h).status_code == 422
            assert (
                cl.post("/event-studies", json={**body, "windows": ["0:900"]}, headers=h).status_code == 422
            )
            ok = cl.post("/event-studies", json=body, headers=h).json()
            assert ok["synthetic"] and ok["notice"].startswith("SYNTHETIC")
            rid = ok["run_id"]
            got = cl.get(f"/event-studies/{rid}").json()
            assert got["result"]["alignment"]["day0"] == "2025-09-17" and got["synthetic"]
            res = cl.get(f"/event-studies/{rid}/results").json()["results"]
            assert {r["window"] for r in res} == {"[0,0]", "[0,+1]", "[-1,+1]", "[0,+5]"}
            rep = cl.get(f"/event-studies/{rid}/report").json()
            assert rep["synthetic"] and "11. Confounding-event audit" in rep["sections"]
            md = cl.get(f"/event-studies/{rid}/report?format=markdown")
            assert md.headers["content-type"].startswith("text/markdown") and "SYNTHETIC DATA" in md.text
            cmp = cl.post("/event-studies/compare", headers=h, json={"event_type": "CENTRAL_BANK", "ticker": "QQQ",
                                                                      "max_events": 5}).json()  # fmt: skip
            assert cmp["kind"] == "compare" and cmp["synthetic"]
            assert cl.post("/event-studies/compare", headers=h,
                           json={"event_type": "CENTRAL_BANK", "ticker": "QQQ", "max_events": 500}).status_code == 422  # fmt: skip
            assert any(x["run_id"] == rid for x in cl.get("/event-studies").json()["runs"])
    finally:
        study_ctx.settings = old


def test_cli_study_commands(study_ctx, monkeypatch):
    from alembic import command

    from finance_agent.config.settings import get_settings
    from finance_agent.interfaces.cli import app
    from finance_agent.storage.db import alembic_config

    # the fixture DB was built with create_all(); record the schema head so the CLI's migration is a no-op
    command.stamp(alembic_config(study_ctx.settings.database_url), "head")
    monkeypatch.setenv("DATABASE_URL", study_ctx.settings.database_url)
    monkeypatch.setenv("DATA_DIR", str(study_ctx.settings.data_dir))
    get_settings.cache_clear()
    r = CliRunner()
    try:
        out = r.invoke(app, ["study", "run", "--event-id", FOMC_SEP25, "--ticker", "QQQ", "--benchmark", "SPY",
                             "--model", "market-model", "--no-robustness", "--offline"])  # fmt: skip
        assert out.exit_code == 0, out.output
        assert "SYNTHETIC DATA" in out.stdout and "## 10. Statistical inference" in out.stdout
        rid = re.search(r"Run `(ES-[0-9a-f]{16})`", out.stdout).group(1)
        rep = r.invoke(app, ["study", "report", "--run-id", rid, "--offline", "--format", "json"])
        assert rep.exit_code == 0 and json.loads(rep.stdout)["run_id"] == rid
        y = r.invoke(app, ["study", "run", "--event-id", FOMC_SEP25, "--series", "DGS10", "--offline"])
        assert y.exit_code == 0 and "basis points" in y.stdout
        cmp = r.invoke(app, ["study", "compare", "--event-type", "MACRO_RELEASE", "--subtype", "CPI", "--ticker",
                             "QQQ", "--benchmark", "SPY", "--start", "2025-07-01", "--offline"])  # fmt: skip
        assert cmp.exit_code == 0 and "Multi-event study" in cmp.stdout
        bad = r.invoke(
            app, ["study", "run", "--event-id", FOMC_SEP25, "--ticker", "QQQ", "--model", "ml", "--offline"]
        )
        assert bad.exit_code == 1
        lst = r.invoke(app, ["study", "list", "--offline"])
        assert lst.exit_code == 0 and rid in lst.stdout
    finally:
        get_settings.cache_clear()


def test_point_in_time_cutoff_must_be_timezone_aware(study_ctx):
    """Point-in-time cutoffs must be timezone-aware (naive datetimes are rejected by the spec)."""
    with pytest.raises(ValueError):
        run(study_ctx, FOMC_SEP25, ticker="QQQ", data_mode="point_in_time", as_of=datetime(2026, 1, 1))
    # cutoff after the fixtures' recorded retrieval (2026-10-30 18:00Z): availability is verified
    assert run(study_ctx, FOMC_SEP25, ticker="QQQ", data_mode="point_in_time",
               as_of=datetime(2026, 11, 1, tzinfo=UTC)).status == "COMPLETED"  # fmt: skip


def test_robustness_request_is_a_distinct_run(study_ctx):
    """Regression: a run requested WITH robustness checks must not return a stored run without them."""
    plain = run(study_ctx, FOMC_SEP25, ticker="QQQ", benchmark="SPY", windows=["0:3"], robustness=False)
    full = run(study_ctx, FOMC_SEP25, ticker="QQQ", benchmark="SPY", windows=["0:3"], robustness=True)
    assert full.run_id != plain.run_id and not full.existing
    assert plain.result["robustness"] == [] and len(full.result["robustness"]) >= 5
    again = run(study_ctx, FOMC_SEP25, ticker="QQQ", benchmark="SPY", windows=["0:3"], robustness=True)
    assert again.existing and again.run_id == full.run_id


def test_yield_window_beyond_published_data_is_out_of_range_not_shortened(study_ctx):
    """Regression: windows past the last published yield are OUT_OF_RANGE; the others still report.

    The offline ALFRED vintage is dated 2026-11-01, so its last published observation is 2026-10-29."""
    r = run(study_ctx, MSFT_EARN, series_id="DGS10")  # 16:04 ET on 10-28 -> yield day 0 = 2026-10-29
    st = {w["window"]: w["status"] for w in r.result["windows"]}
    assert r.status == "COMPLETED" and r.result["day0"] == "2026-10-29" and st["[0,0]"] == "VALID"
    for label in ("[0,+1]", "[-1,+1]", "[0,+5]"):
        assert st[label] == "OUT_OF_RANGE"
    assert (
        "last available 2026-10-29" in win(r, "[0,+5]")["reason"]
        and "not shortened" in win(r, "[0,+5]")["reason"]
    )


def test_yield_report_is_labelled_in_basis_points_and_renders_tables(study_ctx):
    """Regression: the yield report must not reuse the equity CAR heading or break its Markdown table."""
    md = run(study_ctx, FOMC_SEP25, series_id="DGS2").report_markdown
    assert "## 9. Yield changes around the event (basis points" in md and "Abnormal returns and CAR" not in md
    header = next(line for line in md.splitlines() if line.startswith("| window | status | before"))
    assert header.count("|") == 7  # six columns: no stray pipes inside a header


def test_event_outside_default_ingest_window_gets_actionable_error(tmp_path):
    """Regression (release audit): after the README's default `events ingest --offline` (120-day
    window ending 2026-11-01), a study of the 2025-09-17 FOMC event failed with "not found (run
    `events ingest` first)" although ingest had just run. The error must name the window and the
    exact command that covers the event; after that command the same request succeeds."""
    from finance_agent.errors import NotFoundError
    from finance_agent.events.service import EventIngestor
    from finance_agent.services import AppContext
    from finance_agent.storage.db import Database
    from tests.conftest import fixture_window, offline_settings

    s = offline_settings(tmp_path)
    db = Database(s.database_url)
    db.create_all()
    ctx = AppContext(s, db=db)
    try:
        start, as_of = fixture_window()
        EventIngestor(ctx).run(as_of=as_of)  # default window, as documented for v0.2
        req = EventStudyRequest(event_id=FOMC_SEP25, series_id="DGS10", price_basis="total_return")
        with pytest.raises(NotFoundError) as exc:
            EventStudyService(ctx).run(req)
        msg = str(exc.value)
        assert "120 days" in msg and f"events ingest --offline --start {start.isoformat()}" in msg
        assert "run `events ingest` first" not in msg
        EventIngestor(ctx).run(as_of=as_of, start=start)  # the command the message names
        assert EventStore(ctx.db).get(FOMC_SEP25) is not None
    finally:
        ctx.close()
