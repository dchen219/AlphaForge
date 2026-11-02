# Agent Testing Console (Streamlit)

A **local** developer and research console for exercising the implemented AlphaForge v0.1 and
v0.2 capabilities through the real FastAPI application. It is not a production app. It does not
deploy, trade, ingest, or add any financial or event logic: every number, ranking and visibility
decision comes from the backend.

## Installation

Streamlit lives in an optional `ui` dependency group, which `uv sync` installs by default
(`[tool.uv] default-groups = ["dev", "ui"]`). The core package does not depend on it, so pandas and
NumPy enter only through Streamlit.

```bash
uv sync                      # installs dev + ui groups
uv sync --no-group ui        # core + dev only (the console is then unavailable)
```

## Required services

| Service | Command | Binds |
|---|---|---|
| Offline API (recorded fixtures) | `uv run finance-agent serve --offline --port 8000` | 127.0.0.1:8000 |
| Live API (optional) | `SEC_USER_AGENT="Name email" uv run finance-agent serve --port 8001` | 127.0.0.1:8001 |
| Console | `uv run python -m streamlit run apps/ui/app.py` | 127.0.0.1:8501 |

The **event database must be populated first** (the console never ingests):

```bash
uv run finance-agent init-db
uv run finance-agent events ingest --offline     # real recorded Fed/BLS/BEA/SEC snapshots
```

Use `python -m streamlit` rather than `streamlit`: on machines with Windows Application Control,
the `streamlit.exe` / `pytest.exe` shims in `.venv\Scripts` may be blocked.

Backend URLs come only from environment variables, never from the browser:
`ALPHAFORGE_API_URL` (offline, default `http://127.0.0.1:8000`) and `ALPHAFORGE_LIVE_API_URL`
(live, default `http://127.0.0.1:8001`). Non-loopback hosts, credentials in the URL, queries and
fragments are rejected.

### Launch (copy-paste, PowerShell)

```powershell
uv sync
uv run finance-agent init-db
uv run finance-agent events ingest --offline
Start-Process -NoNewWindow uv -ArgumentList "run","finance-agent","serve","--offline","--port","8000"
uv run python -m streamlit run apps/ui/app.py      # open http://127.0.0.1:8501
```

Offline and live servers share `DATABASE_URL` unless you set it differently per process. Every
record carries its `data_origin`, so mixing is labelled, but a separate offline database
(`$env:DATABASE_URL="sqlite:///data/offline.db"` for both the ingest and the server) keeps
fixture data apart.

## Data mode (offline / live)

Offline and live are **server-level** settings (`AppContext` is built per process), so the console
selects between the two configured backends rather than flipping a flag per request. On every page
render, the console calls `GET /health` and **refuses to send requests** if the server's
`offline_mode` does not match the selected mode. An offline server can therefore never be
presented as live.

Every result shows a data-origin banner:

| Origin | Banner |
|---|---|
| `live` | green: LIVE, retrieved from the official source |
| `fixture:real_snapshot` | blue: HISTORICAL REAL SNAPSHOT (offline fixture) |
| `fixture:synthetic` (or any mix that includes it) | red: SYNTHETIC FIXTURE, NOT real findings |
| missing / unknown | amber: MISSING / UNVERIFIED ORIGIN |

Mixed origins are reduced to the most cautious member (synthetic > unknown > snapshot > live).

## Pages and backend mapping

| Page | Controls | API calls | Backend modules |
|---|---|---|---|
| **A. Company Research** | ticker, quarters, as-of date/time, LLM on/off, mode | `GET /companies/{t}`, `GET /companies/{t}/financials`, `POST /research/company` | `services.AppContext`, `research/*`, `workflows/company_research.py`, `verification/checks.py` |
| **B. Financial Events** | date range, source, category, subtype, ticker, importance level, cutoff | `GET /events`, `GET /events/{id}?as_of=`, `GET /events/calendar` | `events/store.py` (`query`, `revision_chain`, `mask_future_links`), `events/importance.py` |
| **C. Daily Briefing** | date, explicit cutoff, window hours, max macro/corporate, tickers, LLM, mode; load stored | `POST /intelligence/briefing`, `GET /intelligence/briefings`, `GET /intelligence/briefings/{id}` | `workflows/briefing.py`, `events/briefing.py` |
| **D. Historical Replay** | cutoff date/time, look-back days, diagnostic horizon, category, ticker, snapshot/live data | `GET /events/replay` | `events/replay.py` (`replay_audit`, `calendar_visibility`) |
| **E. Agent Execution** | workflow type, run picker | `GET /workflows`, `GET /research`, `GET /research/{id}`, `GET /intelligence/briefings[/{id}]` | `storage/repository.py` (`list_runs`), `events/store.py` (`BriefingStore.recent`) |

Charts on Page A plot API values only. Ratios are multiplied by 100 and USD values are divided by
1e9 for display; nothing is computed.

## API additions (read-only, backward compatible)

All additions are GET routes or new optional response fields. Existing routes and fields are
unchanged.

- `GET /events/replay?as_of=…&from=…&horizon_days=…&category=…&ticker=…`: visibility audit.
  `visible` is exactly the result of `EventStore.query(as_of=…)`, the same call the briefing uses.
  `excluded_future` lists events available after the cutoff, and `excluded_no_availability_time`
  lists published events with no usable time; both are look-ahead diagnostics only. `calendar`
  gives each entry's status (`known_upcoming`, `known_due_confirmed`, `known_due_unconfirmed`,
  `withheld_observed_later`) and the time it was first observed. `masked_links` lists links that
  the point-in-time masking hid.
- `GET /events/{id}` now accepts an optional `as_of`. It returns 404 if the event was not
  available at the cutoff, masks later links, filters cluster members, and returns a
  `revision_history` filtered to the cutoff.
- `GET /events` accepts `subtype` (repeatable).
- `GET /research?limit=&status=` and `GET /intelligence/briefings?limit=` list stored runs.
- `GET /workflows` returns the planned node order of both LangGraph workflows.
- `ResearchRunOut` gains `as_of` and `params`. `BriefingRunOut` gains `as_of`, `window_*`,
  `created_at`, `completed_at` and `params`. `/health` gains `fixture_event_window` (offline only).

`events/replay.calendar_visibility` was extracted from `BriefingWorkflow.retrieve` with identical
logic, so the replay audit and the briefing share one implementation. A test asserts that their
withheld counts agree.

## Inspecting LangGraph execution

Both workflows are bounded linear graphs (`RESEARCH_NODES`, `BRIEFING_NODES`); any node error
routes to `fail`. Each node wrapper records one step `{step, status, ms | error}`. For
observability, the wrapper now also records `started_at` and `finished_at` (UTC ISO). This changes
no workflow logic. Page E joins the planned nodes with the recorded steps:

- **executed / not executed**: a node after a failure shows "not executed"; nothing is inferred.
- **duration_ms (measured)**: `time.perf_counter()` around the node function.
- **started_at / finished_at**: only for runs recorded after this change; older runs show
  "not recorded".
- **total duration**: from the stored `created_at` / `completed_at` of the run.
- **providers / provenance**: research shows snapshot URL host, origin, retrieval time and SHA-256.
  Briefings show evidence records per source.
- **LLM vs fallback**: `llm.provider` / `llm.status` (`generated` | `unavailable` | `failed`) and
  notes. **Verification**: every check with its details.

Research steps are persisted after every node. Briefing steps are persisted when the run
completes or fails, and a briefing that fails before its window is resolved is not stored (the
POST response still returns the error).

## Security

- Streamlit binds to `127.0.0.1` (`.streamlit/config.toml`), the API binds to `127.0.0.1` by
  default (`finance-agent serve`), usage statistics are off, and XSRF protection is on.
- The console sends requests only to loopback backends taken from the environment, and does not
  follow redirects.
- No secrets reach the browser: `/health` exposes credential presence as booleans only, and the UI
  reads no settings.
- There is no shell access, no CLI subprocess, no ingestion, no destructive database operation and
  no admin token handling. `POST /intelligence/ingest` stays disabled unless
  `INGEST_ADMIN_TOKEN` is set, and the UI never calls it.
- Public deployment is out of scope.

## Tests

```bash
uv run python -m pytest tests/ui                                 # client, validation, labels, pages
uv run python -m pytest tests/historical_replay/test_replay_audit.py
```

- `tests/ui/test_client.py`: mocked HTTP (`httpx.MockTransport`) covering parsing, query params,
  4xx/5xx/validation errors, non-JSON bodies, contract drift, unreachable hosts, timeouts,
  redirects and host allow-list.
- `tests/ui/test_validation_labels.py`: form validation, cutoff semantics, synthetic/mixed/unknown
  origin labels.
- `tests/ui/test_pages.py`: Streamlit `AppTest` against the **real API on a loopback port**,
  backed by a database ingested from the real recorded fixtures. Covers all five pages, the
  navigation entry point, offline research without Anthropic credentials, cutoff integrity, the
  synthetic-event banner, unreachable live backend, mode mismatch and remote-URL rejection.
- `tests/historical_replay/test_replay_audit.py`: visible set equals the as_of query; excluded
  events are strictly after the cutoff; calendar knowledge time; agreement with the briefing; and
  SYNTHETIC revision-chain and masked-link cases.

Screenshots are not included: no headless browser is available in the build environment. The
`AppTest` suite provides the rendering evidence.

## Known limitations

- Live mode needs a second API process (`--port 8001`, `SEC_USER_AGENT`). Live research and live
  LLM synthesis were not exercised while building the console (no Anthropic credit).
- Research and briefing endpoints are synchronous. The client waits up to 600 s, with no progress
  streaming or cancellation.
- Per-node tool/HTTP calls and LLM token usage are not traced. Per-node timestamps exist only for
  runs made after this change.
- Source options on Page B are a fixed list of the source names the v0.2 ingesters write.
- The event calendar listing on Page B is not cutoff-filtered; use Page D for knowledge-time views.
- `GET /events/{id}?as_of=` filters revision history and links, but source records are shown with
  their actual retrieval times, which may be later than the cutoff.
- Macro series (`GET /macro`, synthetic FRED fixtures offline) are not surfaced on a page.
