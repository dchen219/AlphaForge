"""Record REAL v0.2 offline fixtures by running the actual ingestion code live.

Every official response fetched during a live ingestion run is written to the fixture
path that ``FixtureTransport`` will later serve (offline replay = same bytes, same code).

Recorded (real official sources, robots.txt-permitted paths):
  sec/submissions/CIK*.json   SEC EDGAR submissions (periodic + 8-K forms, items, acceptance times)
  fed/*                       FOMC calendar, monetary press RSS, policy statements
  bls/bls.ics                 BLS release calendar
  bea/schedule.htm            BEA release schedule
  alfred/<SERIES>/<DATE>.csv  ALFRED vintages (keyless CSV)

Trimming (content values are never altered): ALFRED rows before 2015 are dropped;
HTML has <script>/<style> blocks and comments removed; SEC submissions keep only event
forms and the columns the code reads.

Usage:  SEC_USER_AGENT="Name email" uv run python scripts/build_v02_fixtures.py [--start 2025-07-01]
Respects provider rate limits (ALFRED crawl-delay 2 s), so a full run takes ~15 minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from finance_agent.config.settings import Settings  # noqa: E402
from finance_agent.events.service import EventIngestor  # noqa: E402
from finance_agent.events.sources.sec_events import EVENT_FORMS  # noqa: E402
from finance_agent.http import FixtureTransport  # noqa: E402
from finance_agent.services import AppContext  # noqa: E402
from finance_agent.storage.db import Database  # noqa: E402
from finance_agent.timeutil import ET, parse_as_of  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
RECORD_PREFIXES = ("sec/submissions/", "fed/", "bls/", "bea/", "alfred/")
SUB_COLUMNS = [
    "accessionNumber",
    "filingDate",
    "reportDate",
    "acceptanceDateTime",
    "form",
    "items",
    "primaryDocument",
    "primaryDocDescription",
]


def trim(rel: str, body: bytes) -> bytes:
    if rel.startswith("alfred/"):
        lines = body.decode("utf-8-sig").splitlines()
        kept = [lines[0]] + [ln for ln in lines[1:] if ln[:10] >= "2015-01-01"]
        return ("\n".join(kept) + "\n").encode()
    if rel.endswith((".htm", ".html")):
        t = body.decode("utf-8", errors="replace")
        t = re.sub(r"(?is)<(script|style|svg|noscript)[^>]*>.*?</\1>", "", t)
        t = re.sub(r"(?s)<!--.*?-->", "", t)
        t = re.sub(r"[ \t]+", " ", t)
        t = re.sub(r"\n\s*\n+", "\n", t)
        return t.encode()
    if rel.startswith("sec/submissions/"):
        d = json.loads(body)
        r = d["filings"]["recent"]
        idx = [i for i, f in enumerate(r["form"]) if f in EVENT_FORMS]
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
        out["filings"] = {"recent": {c: [r[c][i] for i in idx] for c in SUB_COLUMNS if c in r}, "files": []}
        return json.dumps(out, separators=(",", ":")).encode()
    return body


class RecordingTransport(httpx.BaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.HTTPTransport(retries=0)
        self.mapper = FixtureTransport(FIX)
        self.recorded: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        resp = self.inner.handle_request(request)
        resp.read()
        path = self.mapper.resolve(request.url)
        if resp.status_code == 200 and path is not None:
            rel = path.relative_to(FIX).as_posix()
            if rel.startswith(RECORD_PREFIXES):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(trim(rel, resp.content))
                self.recorded.append(rel)
        return resp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2025-07-01")
    ap.add_argument("--workdir", default=str(ROOT / "data" / "fixture_build"))
    a = ap.parse_args()
    if not os.environ.get("SEC_USER_AGENT"):
        sys.exit("SEC_USER_AGENT is required (SEC fair-access policy).")
    work = Path(a.workdir)
    work.mkdir(parents=True, exist_ok=True)
    s = Settings(
        _env_file=None,
        offline_mode=False,
        sec_user_agent=os.environ["SEC_USER_AGENT"],
        database_url=f"sqlite:///{(work / 'build.db').as_posix()}",
        data_dir=work,
        llm_enabled=False,
    )
    db = Database(s.database_url)
    db.create_all()
    rec = RecordingTransport()
    ctx = AppContext(s, db=db, transport=rec)
    started = datetime.now(UTC)
    report = EventIngestor(ctx).run(start=date.fromisoformat(a.start))
    print(json.dumps(report.to_dict(), indent=2))
    print(f"recorded {len(set(rec.recorded))} fixture files; ALFRED requests: {ctx.http.request_count}")
    manifest_path = FIX / "MANIFEST.json"
    m = json.loads(manifest_path.read_text(encoding="utf-8"))
    when = started.isoformat(timespec="seconds")
    for prefix, desc in {
        "fed/": "REAL Federal Reserve FOMC calendar, monetary-policy press RSS and policy statements (scripts/style stripped).",
        "bls/": "REAL BLS release calendar (iCalendar).",
        "bea/": "REAL BEA release schedule page (scripts/style stripped).",
        "alfred/": "REAL ALFRED vintage CSVs (keyless alfredgraph.csv), rows before 2015 dropped.",
    }.items():
        m["groups"][prefix] = {
            "kind": "real_snapshot",
            "description": desc,
            "retrieved_at": when,
            "built_by": "scripts/build_v02_fixtures.py",
        }
    m["event_window"] = {
        "start": a.start,
        "as_of": parse_as_of(started.astimezone(ET).date()).isoformat(),
        "note": "Window used by scripts/build_v02_fixtures.py; offline macro replay probes exactly these vintages.",
    }
    m["groups"]["sec/"]["submissions_refreshed_at"] = when
    m["groups"]["sec/"]["submissions_note"] = (
        "sec/submissions re-recorded for v0.2 with 8-K forms, 'items' and "
        "acceptanceDateTime columns; companyfacts unchanged."
    )
    manifest_path.write_text(json.dumps(m, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
