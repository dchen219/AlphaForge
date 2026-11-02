"""Template-based event-study reports (JSON sections + Markdown). No LLM; every number is copied
from the deterministic result. Synthetic inputs produce a prominent banner in both formats."""

from __future__ import annotations

from typing import Any

SYNTHETIC_BANNER = (
    "SYNTHETIC DATA - METHODOLOGICAL VALIDATION ONLY. At least one input (offline equity prices) is "
    "synthetic. These estimates are NOT empirical findings about actual market responses and must "
    "not be used as investment evidence."
)
REAL_BANNER = (
    "Inputs are recorded/live source data. Results are descriptive and statistical associations; "
    "they do not identify causal effects."
)

SECTION_TITLES = [
    "1. Research question",
    "2. Event definition and source",
    "3. Asset and benchmark",
    "4. Data origin and adjustment basis",
    "5. Event time and trading-session alignment",
    "6. Estimation and event-window definitions",
    "7. Expected-return model",
    "8. Raw returns",
    "9. Abnormal returns and CAR",
    "10. Statistical inference",
    "11. Confounding-event audit",
    "12. Robustness results",
    "13. Limitations",
    "14. Reproducibility manifest",
]


def pct(x: Any, nd: int = 4) -> str:
    return "n/a" if x is None else f"{x * 100:+.{nd}f}%"


def num(x: Any, nd: int = 4) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}g}" if abs(x) < 1e-3 and x != 0 else f"{x:.{nd}f}"
    return str(x)


def bp(x: Any) -> str:
    return "n/a" if x is None else f"{x:+.1f} bp"


def _table(header: list[str], rows: list[list[Any]]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def build(run: dict) -> tuple[dict, str]:
    kind = run["kind"]
    if kind == "compare":
        return _compare(run)
    if kind == "yield":
        return _yield(run)
    return _single(run)


def _head(run: dict, title: str) -> list[str]:
    banner = SYNTHETIC_BANNER if run["synthetic"] else REAL_BANNER
    return [
        f"# {title}",
        "",
        f"> **{banner}**",
        "",
        f"Run `{run['run_id']}` - status **{run['status']}** - data origin `{run['data_origin']}` - "
        f"methodology `{run['methodology_version']}`",
        "",
    ]


def _single(run: dict) -> tuple[dict, str]:
    spec, res = run["specification"], run["result"]
    ev = res.get("event", {})
    al = res.get("alignment") or {}
    est = res.get("estimation") or {}
    wins = res.get("windows", [])
    bench = (spec.get("benchmark") or {}).get("instrument_id")
    model = spec.get("model")
    s: dict[str, Any] = {}
    s[SECTION_TITLES[0]] = (
        f"How did {spec['target']} perform around {ev.get('title', spec['event_id'])} "
        f"({ev.get('event_subtype')}), and was the move unusual relative to "
        f"{'the ' + model + ' benchmark model on ' + bench if bench and model != 'raw' else 'its own history'}?"
    )
    s[SECTION_TITLES[1]] = {k: ev.get(k) for k in (
        "event_id", "title", "event_type", "event_subtype", "source_name", "source_url",
        "source_document_id", "published_at", "available_at", "availability_precision", "data_origin",
    )}  # fmt: skip
    s[SECTION_TITLES[2]] = {"target": spec["target"], "benchmark": spec.get("benchmark")}
    m = res.get("data_manifest", {}).get("target") or {}
    s[SECTION_TITLES[3]] = {
        "data_origin": run["data_origin"],
        "synthetic": run["synthetic"],
        "price_basis": spec.get("price_basis"),
        "return_type": spec.get("return_type"),
        "data_mode": spec.get("data_mode"),
        "as_of": spec.get("as_of"),
        "contract_statement": m.get("statement"),
        "retrospectively_adjusted": m.get("retrospectively_adjusted"),
        "adjustment_method": m.get("adjustment_method"),
        "quality_exclusions": spec.get("quality_exclusions"),
    }
    s[SECTION_TITLES[4]] = al
    s[SECTION_TITLES[5]] = {
        "estimation_window": spec.get("estimation_window"),
        "valid_estimation_observations": est.get("valid_observations"),
        "missing_estimation_sessions": len(est.get("missing_sessions") or []),
        "event_windows": [{"window": w["window"], "sessions": w.get("sessions")} for w in wins],
    }
    s[SECTION_TITLES[6]] = {
        "model": model,
        **{k: est.get(k) for k in ("alpha", "alpha_se", "beta", "beta_se", "residual_variance",
                                    "residual_df", "r_squared", "status")},
    }  # fmt: skip
    s[SECTION_TITLES[7]] = [
        {
            k: w.get(k)
            for k in ("window", "status", "raw_cumulative_return", "benchmark_cumulative_return", "bhar")
        }
        for w in wins
    ]
    s[SECTION_TITLES[8]] = {
        "daily": res.get("observations", []),
        "windows": [{k: w.get(k) for k in ("window", "status", "reason", "car")} for w in wins],
        "note": "CAR is the arithmetic sum of abnormal returns, not a compounded investment return.",
    }
    s[SECTION_TITLES[9]] = [{"window": w["window"], **(w.get("inference") or {})} for w in wins]
    s[SECTION_TITLES[10]] = res.get("confounders")
    s[SECTION_TITLES[11]] = res.get("robustness", [])
    s[SECTION_TITLES[12]] = {
        "interpretation_scope": res.get("interpretation_scope"),
        "warnings": res.get("warnings", []) + est.get("warnings", []),
        "status_reason": res.get("status_reason"),
    }
    s[SECTION_TITLES[13]] = _manifest(run)

    md = _head(run, f"Event study: {spec['target']} around {ev.get('event_subtype', '')} {spec['event_id']}")
    md += [f"## {SECTION_TITLES[0]}", s[SECTION_TITLES[0]], ""]
    md += [
        f"## {SECTION_TITLES[1]}",
        _table(["field", "value"], [[k, v] for k, v in s[SECTION_TITLES[1]].items()]),
        "",
    ]
    md += [f"## {SECTION_TITLES[2]}", f"Target `{spec['target']}`; benchmark "
           f"`{bench or 'none'}` - {(spec.get('benchmark') or {}).get('description', '')}", ""]  # fmt: skip
    md += [
        f"## {SECTION_TITLES[3]}",
        _table(["field", "value"], [[k, v] for k, v in s[SECTION_TITLES[3]].items()]),
        "",
    ]
    md += [f"## {SECTION_TITLES[4]}",
           f"Timing class **{al.get('timing_class')}** ({al.get('time_basis')}); event time "
           f"{al.get('event_time_local') or 'unknown'}; day 0 = **{al.get('day0')}**. {al.get('rule')}"]  # fmt: skip
    if al.get("contamination_warning"):
        md.append(f"\n**Warning:** {al['contamination_warning']}.")
    md += [*(f"- {n}" for n in al.get("notes", [])), ""]
    ew = spec.get("estimation_window") or {}
    md += [f"## {SECTION_TITLES[5]}",
           f"Estimation offsets [{ew.get('start')}, {ew.get('end')}] (minimum {ew.get('min_observations')} valid "
           f"paired observations; {est.get('valid_observations')} valid, "
           f"{len(est.get('missing_sessions') or [])} missing - never filled). Event windows in trading sessions:", ""]  # fmt: skip
    md += [_table(["window", "first session", "last session", "status"],
                  [[w["window"], (w.get("sessions") or ["?"])[0], (w.get("sessions") or ["?"])[-1], w["status"]] for w in wins]), ""]  # fmt: skip
    md += [f"## {SECTION_TITLES[6]}", f"Model **{model}**.",
           _table(["alpha", "se(alpha)", "beta", "se(beta)", "sigma^2", "df", "R^2"],
                  [[num(est.get("alpha"), 6), num(est.get("alpha_se"), 6), num(est.get("beta")),
                    num(est.get("beta_se")), num(est.get("residual_variance"), 6), est.get("residual_df"),
                    num(est.get("r_squared"))]]), ""]  # fmt: skip
    md += [f"## {SECTION_TITLES[7]}", _table(["window", "status", "asset cumulative", "benchmark cumulative", "BHAR"],
           [[w["window"], w["status"], pct(w.get("raw_cumulative_return")), pct(w.get("benchmark_cumulative_return")),
             pct(w.get("bhar"))] for w in wins]), ""]  # fmt: skip
    obs = res.get("observations", [])
    md += [f"## {SECTION_TITLES[8]}", _table(["rel", "session", "R_i", "R_m", "E[R_i]", "AR", "note"],
           [[o["relative_session"], o["session"], pct(o.get("asset_return")), pct(o.get("benchmark_return")),
             pct(o.get("expected_return")), pct(o.get("abnormal_return")), o.get("note") or ""] for o in obs]), "",
           _table(["window", "CAR", "status / reason"],
                  [[w["window"], pct(w.get("car")), w["status"] + (f" - {w['reason']}" if w.get("reason") else "")] for w in wins]),
           "", "CAR is the arithmetic sum of abnormal returns, not a compounded investment return.", ""]  # fmt: skip
    rows = []
    for w in wins:
        inf = w.get("inference") or {}
        pl = inf.get("placebo") or {}
        rows.append([w["window"], inf.get("status"), num(inf.get("standard_error"), 6),
                     num(inf.get("standard_error_ignoring_estimation_error"), 6), num(inf.get("test_statistic"), 3),
                     inf.get("degrees_of_freedom"), num(inf.get("p_value"), 4),
                     f"[{pct(inf.get('ci_low'))}, {pct(inf.get('ci_high'))}]", num(pl.get("empirical_p_value"), 4)])  # fmt: skip
    md += [f"## {SECTION_TITLES[9]}",
           _table(["window", "status", "SE(CAR)", "SE w/o est. error", "t", "df", "p", "95% CI", "placebo p"], rows), ""]  # fmt: skip
    first_inf = next(
        (w.get("inference") for w in wins if (w.get("inference") or {}).get("assumptions")), None
    )
    if first_inf:
        md += ["Assumptions:", *(f"- {a}" for a in first_inf["assumptions"]), ""]
    for w in wins:
        for x in (w.get("inference") or {}).get("warnings", []):
            md.append(f"- {w['window']}: {x}")
    md.append("")
    md += _confounder_md(res.get("confounders"))
    md += [f"## {SECTION_TITLES[11]}", "All attempted specifications are listed, including failures. "
           "The primary specification was fixed before these were run.", ""]  # fmt: skip
    rb = res.get("robustness", [])
    if rb:
        labels = [w["window"] for w in wins]
        md += [_table(["variant", "status", *[f"CAR {lb}" for lb in labels], *[f"p {lb}" for lb in labels]],
               [[v["variant"], v["status"], *[pct(v["windows"].get(lb, {}).get("car")) for lb in labels],
                 *[num(v["windows"].get(lb, {}).get("p_value"), 3) for lb in labels]] for v in rb]), ""]  # fmt: skip
    else:
        md += ["Robustness checks were not requested or not applicable.", ""]
    md += [
        f"## {SECTION_TITLES[12]}",
        *(f"- {k}: {v}" for k, v in (res.get("interpretation_scope") or {}).items()),
    ]
    md += [*(f"- {x}" for x in s[SECTION_TITLES[12]]["warnings"]), ""]
    md += _manifest_md(s[SECTION_TITLES[13]])
    return s, "\n".join(md)


def _confounder_md(c: dict | None) -> list[str]:
    md = [f"## {SECTION_TITLES[10]}"]
    if not c:
        return [*md, "Not run.", ""]
    md.append(f"Status **{c['status']}**. {c['statement']}")
    for rel in ("confounders", "same_announcement", "derived"):
        items = c.get(rel) or []
        if items:
            md += ["", f"{rel.replace('_', ' ')}:", _table(["rel", "subtype", "title", "time", "importance", "reason"],
                   [[x["relative_session"], x["event_subtype"], x["title"][:60], x.get("published_at") or x["time_basis"],
                     num(x.get("importance_score"), 3), x["reason"]] for x in items])]  # fmt: skip
    md += ["", "Coverage:", _table(["source family", "status", "stored range"],
           [[k, v["status"], v.get("stored_range", "")] for k, v in c["coverage"].items()]),
           "", "Never covered: " + "; ".join(c["not_covered_by_any_source"]), ""]  # fmt: skip
    return md


def _manifest(run: dict) -> dict:
    return {
        "run_id": run["run_id"],
        "spec_hash": run["spec_hash"],
        "data_digest": run["data_digest"],
        "methodology_version": run["methodology_version"],
        "software": run.get("software"),
        "market_data": run["result"].get("data_manifest"),
        "reproduce": run["result"].get("reproduce"),
    }


def _manifest_md(m: dict) -> list[str]:
    md = [f"## {SECTION_TITLES[13]}",
          _table(["item", "value"], [["run id", m["run_id"]], ["specification hash", m["spec_hash"]],
                                     ["data digest", m["data_digest"]], ["methodology", m["methodology_version"]],
                                     ["software", m.get("software")]])]  # fmt: skip
    for role, mm in (m.get("market_data") or {}).items():
        if isinstance(mm, dict) and mm.get("bars_digest"):
            md.append(f"- {role} `{mm['instrument_id']}`: {mm['bars_used']} bars, versions {mm['bar_versions']}, "
                      f"{len(mm['snapshot_sha256'])} snapshot(s), corrected sessions {len(mm['corrected_sessions'])}, "
                      f"adjustment factors {len(mm['adjustment_factors'])}, bars digest `{mm['bars_digest'][:16]}`")  # fmt: skip
    if m.get("reproduce"):
        md += ["", f"Reproduce: `{m['reproduce']}`"]
    return [*md, ""]


def _yield(run: dict) -> tuple[dict, str]:
    spec, res = run["specification"], run["result"]
    ev = res.get("event", {})
    s = {
        SECTION_TITLES[0]: f"How did the {spec['target']} Treasury yield change around "
        f"{ev.get('title', spec['event_id'])}? (yield changes in basis points; no expected-change model)",
        SECTION_TITLES[1]: ev,
        SECTION_TITLES[3]: {
            "data_origin": run["data_origin"],
            "synthetic": run["synthetic"],
            "unit": "basis points (yields are percent per annum)",
            **res.get("data_manifest", {}),
        },
        SECTION_TITLES[4]: {
            **(res.get("alignment") or {}),
            "yield_day0": res.get("day0"),
            "yield_rule": res.get("day0_rule"),
        },
        SECTION_TITLES[8]: res.get("windows", []),
        SECTION_TITLES[10]: res.get("confounders"),
        SECTION_TITLES[12]: {
            "interpretation_scope": res.get("interpretation_scope"),
            "warnings": res.get("warnings"),
        },
        SECTION_TITLES[13]: _manifest(run),
    }
    md = _head(
        run,
        f"Treasury yield event study: {spec['target']} around {ev.get('event_subtype', '')} {spec['event_id']}",
    )
    md += [f"## {SECTION_TITLES[0]}", s[SECTION_TITLES[0]], "",
           f"## {SECTION_TITLES[4]}", f"Event time {(res.get('alignment') or {}).get('event_time_local') or 'date only'}; "
           f"yield day 0 = **{res.get('day0')}** - {res.get('day0_rule')}", "",
           "## 9. Yield changes around the event (basis points; no abnormal-return model)",
           _table(["rel", "date", "yield %", "change", "no-quote days bridged"],
                  [[d["relative_day"], d["date"], num(d["yield_pct"], 2), bp(d["change_bp"]),
                    ", ".join(d["no_quote_days_between"])] for d in res.get("daily_changes", [])]), "",
           _table(["window", "status", "before (date)", "after", "change", "share of pre-event abs. changes >= this"],
                  [[w["window"], w["status"], f"{num(w.get('yield_before_pct'), 2)} ({w.get('yield_before_date', '')})",
                    num(w.get("yield_after_pct"), 2), bp(w.get("change_bp")),
                    num((w.get("pre_event_reference") or {}).get("share_with_larger_absolute_change"), 3)]
                   for w in res.get("windows", [])]), "",
           "Yield changes are in basis points of the yield level; they are not price returns (a Treasury "
           "ETF study measures returns in percent).", ""]  # fmt: skip
    md += _confounder_md(res.get("confounders"))
    md += [f"## {SECTION_TITLES[12]}", *(f"- {k}: {v}" for k, v in (res.get("interpretation_scope") or {}).items()),
           *(f"- {w}" for w in res.get("warnings", [])), ""]  # fmt: skip
    md += _manifest_md(s[SECTION_TITLES[13]])
    return s, "\n".join(md)


def _compare(run: dict) -> tuple[dict, str]:
    spec, res = run["specification"], run["result"]
    agg = res.get("aggregate", {})
    s = {
        SECTION_TITLES[0]: f"How did {spec['target']} respond, on average, around {spec.get('category')} "
        f"{spec.get('subtypes') or ''} events relative to {spec.get('benchmark')} ({spec.get('model')})?",
        "selection": res.get("selection"),
        "clusters": res.get("clusters_summary"),
        "aggregate_primary": agg,
        "sensitivity": res.get("sensitivity"),
        "excluded": res.get("excluded"),
        SECTION_TITLES[12]: res.get("limitations"),
        SECTION_TITLES[13]: _manifest(run),
    }
    md = _head(
        run,
        f"Multi-event study: {spec['target']} vs {spec.get('benchmark')} around {spec.get('category')} events",
    )
    sel = res.get("selection", {})
    md += [f"## {SECTION_TITLES[0]}", s[SECTION_TITLES[0]], "",
           "## Sample", _table(["item", "value"], [[k, v] for k, v in sel.items()]), "",
           "## Aggregate (primary specification; unit = independent overlap cluster)"]  # fmt: skip
    rows = []
    for lb, a in agg.items():
        c, r = a["car"], a["raw_cumulative_return"]
        t = a["cross_sectional_test"]
        rows.append([lb, a["clusters_with_valid_window"], pct(r.get("mean")), pct(c.get("mean")), pct(c.get("median")),
                     pct(c.get("sd")), t.get("status"), num(t.get("p_value"), 4)])  # fmt: skip
    md += [
        _table(["window", "clusters", "mean raw", "mean CAR", "median CAR", "sd CAR", "test", "p"], rows),
        "",
    ]
    md += ["Significance is reported only as LIMITED and only with enough independent clusters; events "
           "released together count once, and overlapping windows are merged.", ""]  # fmt: skip
    sens = res.get("sensitivity", {})
    if sens:
        md += ["## Sensitivity (labelled; not the primary result)",
               _table(["analysis", "window", "clusters", "mean CAR"],
                      [[name, lb, a["clusters_with_valid_window"], pct(a["car"].get("mean"))]
                       for name, block in sens.items() if isinstance(block, dict) and "aggregate" in block
                       for lb, a in block["aggregate"].items()]), ""]  # fmt: skip
    md += ["## Events", _table(["event", "subtype", "day 0", "status", "cluster", "confounders"],
           [[e["event_id"], e["event_subtype"], e.get("day0"), e["status"], e.get("cluster_id", ""),
             e.get("confounder_count", 0)] for e in res.get("events", [])]), ""]  # fmt: skip
    md += [f"## {SECTION_TITLES[12]}", *(f"- {x}" for x in res.get("limitations", [])), ""]
    md += _manifest_md(s[SECTION_TITLES[13]])
    return s, "\n".join(md)
