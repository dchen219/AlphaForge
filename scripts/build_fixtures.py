"""Build offline fixtures.

1. SEC (REAL data): downloads company_tickers, submissions and companyfacts for the
   initial companies from official SEC endpoints and trims them to what the
   system uses (supported concepts; periodic-report filings). Requires network and
   SEC_USER_AGENT.
2. FRED (SYNTHETIC data): FRED/ALFRED requires an API key that was not available
   when v0.1 was built, so FRED fixtures are generated deterministically in the
   exact FRED API JSON shape, with realistic vintage/revision structure. Their
   titles are prefixed "[SYNTHETIC FIXTURE]" and they must never be read as real data.

Usage:
    uv run python scripts/build_fixtures.py sec  [--from-dir DIR]
    uv run python scripts/build_fixtures.py fred
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(ROOT / "src"))

from finance_agent.research.concepts import SUPPORTED_CONCEPTS  # noqa: E402
from finance_agent.schemas.sec import PERIODIC_FORMS  # noqa: E402

COMPANIES = {
    "MSFT": "0000789019",
    "AAPL": "0000320193",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "META": "0001326801",
}
EXTRA_TICKERS = {"NVDA", "TSLA", "BRK-B", "JPM", "GOOG"}


def _get(client: httpx.Client, url: str) -> dict:
    for attempt in range(4):
        r = client.get(url)
        if r.status_code in (429, 503):
            time.sleep(2**attempt)
            continue
        r.raise_for_status()
        time.sleep(0.25)
        return r.json()
    raise RuntimeError(f"failed: {url}")


def trim_companyfacts(d: dict) -> dict:
    out = {"cik": d["cik"], "entityName": d["entityName"], "facts": {}}
    for tax, concepts in d["facts"].items():
        if tax != "us-gaap":
            continue
        kept = {}
        for name, body in concepts.items():
            if name not in SUPPORTED_CONCEPTS:
                continue
            units = {
                u: [r for r in rows if r.get("form") in PERIODIC_FORMS and r.get("end", "") >= "2019-01-01"]
                for u, rows in body["units"].items()
            }
            kept[name] = {
                "label": body.get("label"),
                "description": body.get("description"),
                "units": {u: rows for u, rows in units.items() if rows},
            }
        out["facts"][tax] = kept
    return out


def trim_submissions(d: dict) -> dict:
    recent = d["filings"]["recent"]
    idx = [i for i, f in enumerate(recent["form"]) if f in PERIODIC_FORMS]
    keep_cols = [
        "accessionNumber",
        "filingDate",
        "reportDate",
        "acceptanceDateTime",
        "form",
        "primaryDocument",
    ]
    trimmed = {c: [recent[c][i] for i in idx] for c in keep_cols}
    keys = [
        "cik",
        "entityType",
        "sic",
        "sicDescription",
        "name",
        "tickers",
        "exchanges",
        "fiscalYearEnd",
        "stateOfIncorporation",
    ]
    out = {k: d.get(k) for k in keys}
    out["filings"] = {"recent": trimmed, "files": []}
    return out


def build_sec(from_dir: Path | None) -> None:
    retrieved = datetime.now(UTC).isoformat(timespec="seconds")
    if from_dir is not None:  # use the download time of the raw files, not the trim time
        mtime = (from_dir / "tickers.json").stat().st_mtime
        retrieved = datetime.fromtimestamp(mtime, UTC).isoformat(timespec="seconds")
    ua = os.environ.get("SEC_USER_AGENT")
    client = None
    if from_dir is None:
        if not ua:
            sys.exit("SEC_USER_AGENT is required to download SEC data.")
        client = httpx.Client(headers={"User-Agent": ua, "Accept-Encoding": "gzip"}, timeout=60)

    def load(name: str, url: str) -> dict:
        if from_dir is not None:
            return json.loads((from_dir / name).read_text(encoding="utf-8"))
        return _get(client, url)

    tickers = load("tickers.json", "https://www.sec.gov/files/company_tickers.json")
    wanted = set(COMPANIES) | EXTRA_TICKERS
    small = {k: v for k, v in tickers.items() if v["ticker"] in wanted}
    (FIX / "sec").mkdir(parents=True, exist_ok=True)
    (FIX / "sec" / "company_tickers.json").write_text(json.dumps(small, indent=1), encoding="utf-8")
    for ticker, cik in COMPANIES.items():
        facts = load(f"facts_{cik}.json", f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")
        subs = load(f"sub_{cik}.json", f"https://data.sec.gov/submissions/CIK{cik}.json")
        (FIX / "sec" / "companyfacts" / f"CIK{cik}.json").write_text(
            json.dumps(trim_companyfacts(facts), separators=(",", ":")), encoding="utf-8"
        )
        (FIX / "sec" / "submissions" / f"CIK{cik}.json").write_text(
            json.dumps(trim_submissions(subs), separators=(",", ":")), encoding="utf-8"
        )
        print("wrote", ticker)
    _update_manifest(
        "sec/",
        {
            "kind": "real_snapshot",
            "description": "Real SEC EDGAR data (company_tickers, submissions, XBRL companyfacts) for "
            "MSFT, AAPL, AMZN, GOOGL, META, trimmed to supported concepts, periodic "
            "forms and periods ending 2019-01-01 or later.",
            "retrieved_at": retrieved,
            "source": "https://www.sec.gov/files/company_tickers.json; https://data.sec.gov/",
        },
    )


# --------------------------------------------------------------------- FRED (synthetic)

SERIES = {
    # id: (title, units, frequency, base level, per-period drift, noise amplitude, revision amplitude, release lag days)
    "CPIAUCSL": (
        "Consumer Price Index for All Urban Consumers: All Items",
        "Index 1982-1984=100",
        "Monthly",
        300.0,
        0.0025,
        0.3,
        0.05,
        12,
    ),
    "CPILFESL": (
        "Consumer Price Index: All Items Less Food and Energy",
        "Index 1982-1984=100",
        "Monthly",
        305.0,
        0.0027,
        0.2,
        0.04,
        12,
    ),
    "UNRATE": ("Unemployment Rate", "Percent", "Monthly", 3.6, 0.0, 0.15, 0.1, 5),
    "PAYEMS": (
        "All Employees, Total Nonfarm",
        "Thousands of Persons",
        "Monthly",
        155000.0,
        0.0012,
        80.0,
        60.0,
        5,
    ),
    "DGS2": (
        "Market Yield on U.S. Treasury Securities at 2-Year Constant Maturity",
        "Percent",
        "Monthly",
        4.4,
        0.0,
        0.25,
        0.0,
        1,
    ),
    "DGS10": (
        "Market Yield on U.S. Treasury Securities at 10-Year Constant Maturity",
        "Percent",
        "Monthly",
        4.0,
        0.0,
        0.2,
        0.0,
        1,
    ),
    "FEDFUNDS": ("Federal Funds Effective Rate", "Percent", "Monthly", 5.33, 0.0, 0.0, 0.0, 1),
    "GDP": ("Gross Domestic Product", "Billions of Dollars", "Quarterly", 27000.0, 0.012, 60.0, 40.0, 29),
}


def _months(start: date, n: int, step: int = 1) -> list[date]:
    out, y, m = [], start.year, start.month
    for _ in range(n):
        out.append(date(y, m, 1))
        m += step
        y, m = y + (m - 1) // 12, (m - 1) % 12 + 1
    return out


def _wiggle(i: int, seed: int) -> float:
    return math.sin(i * 1.7 + seed) * 0.6 + math.sin(i * 0.31 + seed * 2) * 0.4


def build_fred() -> None:
    """Full ALFRED-style vintage history: initial release + up to two revisions."""
    for seed, (sid, (title, units, freq, base, drift, noise, rev, lag)) in enumerate(SERIES.items()):
        quarterly = freq == "Quarterly"
        periods = _months(date(2023, 1, 1), 8 if quarterly else 24, 3 if quarterly else 1)
        rows = []
        for i, p in enumerate(periods):
            level = base * (1 + drift) ** i + noise * _wiggle(i, seed)
            if sid == "FEDFUNDS":  # policy-rate style step cuts in the last months
                level = base - 0.25 * max(0, i - 19)
            next_p = periods[i + 1] if i + 1 < len(periods) else _months(p, 2, 3 if quarterly else 1)[1]
            first_release = next_p + timedelta(days=lag - 1)
            vintages = [(first_release, level)]
            if rev:  # revisions one and two periods later
                vintages.append((first_release + timedelta(days=30), level + rev * _wiggle(i + 3, seed)))
                vintages.append(
                    (first_release + timedelta(days=60), level + rev * 1.4 * _wiggle(i + 5, seed))
                )
            decimals = (
                1
                if sid in {"UNRATE", "PAYEMS", "GDP"}
                else (2 if sid.startswith("DGS") or sid == "FEDFUNDS" else 3)
            )
            if sid == "PAYEMS":
                decimals = 0
            for j, (vstart, val) in enumerate(vintages):
                vend = (
                    (vintages[j + 1][0] - timedelta(days=1)) if j + 1 < len(vintages) else date(9999, 12, 31)
                )
                value = "." if (sid == "DGS2" and i == 7) else f"{round(val, decimals):.{decimals}f}"
                rows.append(
                    {
                        "realtime_start": vstart.isoformat(),
                        "realtime_end": vend.isoformat(),
                        "date": p.isoformat(),
                        "value": value,
                    }
                )
        rows.sort(key=lambda r: (r["date"], r["realtime_start"]))
        obs = {
            "_fixture_note": "SYNTHETIC FIXTURE — NOT REAL DATA",
            "realtime_start": "1776-07-04",
            "realtime_end": "9999-12-31",
            "observation_start": "1600-01-01",
            "observation_end": "9999-12-31",
            "units": "lin",
            "output_type": 1,
            "file_type": "json",
            "order_by": "observation_date",
            "sort_order": "asc",
            "count": len(rows),
            "offset": 0,
            "limit": 100000,
            "observations": rows,
        }
        meta = {
            "_fixture_note": "SYNTHETIC FIXTURE — NOT REAL DATA",
            "seriess": [
                {
                    "id": sid,
                    "realtime_start": "2025-01-01",
                    "realtime_end": "2025-01-01",
                    "title": f"[SYNTHETIC FIXTURE] {title}",
                    "observation_start": periods[0].isoformat(),
                    "observation_end": periods[-1].isoformat(),
                    "frequency": freq,
                    "frequency_short": freq[0],
                    "units": units,
                    "seasonal_adjustment": "Seasonally Adjusted",
                    "last_updated": "2025-01-01 07:44:02-06",
                    "popularity": 0,
                    "notes": "Synthetic values in FRED API format for offline tests. NOT REAL DATA.",
                }
            ],
        }
        (FIX / "fred" / "observations").mkdir(parents=True, exist_ok=True)
        (FIX / "fred" / "series").mkdir(parents=True, exist_ok=True)
        (FIX / "fred" / "observations" / f"{sid}.json").write_text(
            json.dumps(obs, indent=1), encoding="utf-8"
        )
        (FIX / "fred" / "series" / f"{sid}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        print("wrote", sid, len(rows), "rows")
    _update_manifest(
        "fred/",
        {
            "kind": "synthetic",
            "description": "SYNTHETIC FRED/ALFRED responses generated by scripts/build_fixtures.py in the "
            "FRED API JSON format (with vintage revisions and a missing '.' value). "
            "NOT REAL MACROECONOMIC DATA.",
            "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )


def _update_manifest(prefix: str, meta: dict) -> None:
    path = FIX / "MANIFEST.json"
    m = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"groups": {}}
    m["groups"][prefix] = meta
    path.write_text(json.dumps(m, indent=2), encoding="utf-8")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["sec", "fred"])
    ap.add_argument(
        "--from-dir",
        type=Path,
        default=None,
        help="Use already-downloaded raw SEC JSON files instead of the network.",
    )
    a = ap.parse_args()
    build_sec(a.from_dir) if a.what == "sec" else build_fred()
