"""Multi-event aggregation with conservative handling of dependence.

1. Events released together (same source, category, day 0 and instant/date: e.g. CPI and core
   CPI, payrolls and the unemployment rate, first releases and the revisions published with them)
   form ONE announcement: for a single asset they have identical windows, so counting them
   separately would be pseudo-replication.
2. Announcements whose event-window spans share any session are merged into an OVERLAP CLUSTER
   (union-find); the cluster mean is one observation.
3. Descriptive statistics are computed over clusters. A cross-sectional t-test of the mean CAR is
   reported only with >= MIN_CLUSTERS_FOR_TEST clusters and is labelled LIMITED: it assumes
   independence across non-overlapping windows of one asset, ignores the correlation induced by
   overlapping estimation windows, and is robust to event-induced variance only in the
   Brown-Warner (1985) cross-sectional sense. Multiple assets on the same macro announcement are
   never pooled as independent observations (one target per comparison).
"""

from __future__ import annotations

import math
import statistics
from datetime import date

MIN_CLUSTERS_FOR_TEST = 10


def announcement_key(item: dict) -> tuple:
    a = item["alignment"]
    instant = a.get("event_time") or a.get("local_date")
    return (item["source_name"], item["event_type"], a.get("day0"), str(instant)[:16])


def build_clusters(items: list[dict], grid_span: tuple[int, int]) -> list[dict]:
    """items: eligible per-event results with 'event_id', 'alignment', 'session_span' (dates)."""
    ann: dict[tuple, list[dict]] = {}
    for it in items:
        ann.setdefault(announcement_key(it), []).append(it)
    anns = [
        {"key": k, "members": v, "span": set().union(*(set(m["session_span"]) for m in v))}
        for k, v in ann.items()
    ]
    parent = list(range(len(anns)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(anns)):
        for j in range(i + 1, len(anns)):
            if anns[i]["span"] & anns[j]["span"]:
                parent[find(i)] = find(j)
    groups: dict[int, list[dict]] = {}
    for i, a in enumerate(anns):
        groups.setdefault(find(i), []).append(a)
    out = []
    for n, (_, g) in enumerate(sorted(groups.items(), key=lambda kv: min(min(a["span"]) for a in kv[1]))):
        out.append(
            {
                "cluster_id": f"C{n + 1:03d}",
                "announcements": [
                    {
                        "representative_event_id": a["members"][0]["event_id"],
                        "event_ids": [m["event_id"] for m in a["members"]],
                        "day0": a["members"][0]["alignment"].get("day0"),
                    }
                    for a in g
                ],
                "overlapping": len(g) > 1,
                "results": [a["members"][0] for a in g],  # one representative per announcement
            }
        )
    return out


def _stats(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    out = {
        "n": len(vals),
        "mean": statistics.fmean(vals),
        "median": statistics.median(vals),
        "min": min(vals),
        "max": max(vals),
        "share_positive": sum(v > 0 for v in vals) / len(vals),
    }
    out["sd"] = statistics.stdev(vals) if len(vals) > 1 else None
    return out


def cross_sectional_test(vals: list[float]) -> dict:
    if len(vals) < MIN_CLUSTERS_FOR_TEST:
        return {
            "status": "NOT_REPORTED",
            "reason": f"{len(vals)} independent clusters (< {MIN_CLUSTERS_FOR_TEST}); descriptive statistics only",
        }
    from scipy import stats

    n = len(vals)
    m, sd = statistics.fmean(vals), statistics.stdev(vals)
    if sd == 0:
        return {"status": "NOT_REPORTED", "reason": "zero cross-cluster dispersion"}
    se = sd / math.sqrt(n)
    t = m / se
    q = float(stats.t.ppf(0.975, n - 1))
    return {
        "status": "LIMITED",
        "method": "cross-sectional t-test of the mean cluster CAR (Brown & Warner 1985 style)",
        "null_hypothesis": "mean CAR across independent clusters = 0",
        "mean": m,
        "standard_error": se,
        "test_statistic": t,
        "degrees_of_freedom": n - 1,
        "p_value": float(2 * stats.t.sf(abs(t), n - 1)),
        "ci_low": m - q * se,
        "ci_high": m + q * se,
        "assumptions": [
            "cluster CARs are independent (windows do not overlap after clustering)",
            "overlapping estimation windows induce some correlation that is ignored",
            "the clusters are a predefined sample, not selected after looking at outcomes",
        ],
    }


def aggregate(clusters: list[dict], window_labels: list[str]) -> dict:
    out = {}
    for label in window_labels:
        cars, raws, bhars = [], [], []
        for c in clusters:
            vals = [
                w
                for r in c["results"]
                for w in r["windows"]
                if w["window"] == label and w["status"] == "VALID"
            ]
            if not vals:
                continue
            raws.append(statistics.fmean(w["raw_cumulative_return"] for w in vals))
            car_vals = [w["car"] for w in vals if w.get("car") is not None]
            if car_vals:
                cars.append(statistics.fmean(car_vals))
            bh = [w["bhar"] for w in vals if w.get("bhar") is not None]
            if bh:
                bhars.append(statistics.fmean(bh))
        length = None
        for c in clusters:
            for r in c["results"]:
                for w in r["windows"]:
                    if w["window"] == label:
                        length = w["end"] - w["start"] + 1
        car_stats = _stats(cars)
        if car_stats.get("n") and length:
            car_stats["mean_abnormal_return_per_session"] = car_stats["mean"] / length
        out[label] = {
            "clusters_with_valid_window": len(raws),
            "raw_cumulative_return": _stats(raws),
            "car": car_stats,
            "bhar": _stats(bhars),
            "cross_sectional_test": cross_sectional_test(cars) if cars else {"status": "NOT_REPORTED"},
        }
    return out


def span_dates(windows: list[dict]) -> list[date]:
    return sorted({d for w in windows for d in w.get("sessions", [])})
