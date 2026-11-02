# Market-data provider evaluation (v0.3-A)

Date of assessment: 2026-11-02. Scope: daily U.S. equity and ETF bars for reproducible,
point-in-time quantitative research. Official documentation was preferred. Claims taken only
from secondary sources are marked **(secondary)**; claims not verified at all are marked
**(unverified)**. No subscription was purchased and no account was created.

## What was checked directly

| Probe (2026-11-02) | Result |
|---|---|
| `GET data.alpaca.markets/v2/stocks/bars` without keys | HTTP 401: credentials required |
| `GET api.massive.com/v2/aggs/...` without key | HTTP 401: key required |
| `GET stooq.com/q/d/l/?s=spy.us&i=d` | HTML page with a JavaScript browser challenge, no CSV. Not circumvented. |
| Credentials for Alpaca / Massive / Nasdaq Data Link in this environment | none configured |

So **no equity price provider is accessible live** from this environment today. The keyless
ALFRED endpoint (Treasury yields) is accessible and is used live and in fixtures.

## Comparison

| | Alpaca Market Data | Massive (formerly Polygon.io) | Stooq | Nasdaq Data Link | yfinance |
|---|---|---|---|---|---|
| Daily OHLCV | yes, `GET /v2/stocks/bars?timeframe=1Day` | yes, aggregates (custom bars) | CSV download | dataset-dependent (premium EOD) | yes (unofficial) |
| Unadjusted **and** adjusted | explicit `adjustment` = `raw` (default), `split`, `dividend`, `spin-off`, `all` | `adjusted=true` (default) / `false`. The docs describe split adjustment, but dividend treatment is not documented on the bars page. | adjusted series **(secondary)** | dataset-dependent | auto-adjust flags; convention undocumented |
| Corporate actions | `GET /v1/corporate-actions`: forward/reverse splits, cash dividends, name changes, spin-offs, mergers. Fields: `ex_date`, `record_date`, `payable_date`, `process_date`, rates. **No declaration/announcement date.** | splits/dividends reference endpoints exist **(secondary)** | none | dataset-dependent | partial |
| ETFs + equities | yes | yes | yes | dataset-dependent | yes |
| Delisted securities | **(unverified)** | **(unverified)** | **(unverified)** | Sharadar marketed as survivorship-bias-free **(secondary)** | no guarantee |
| Authentication | API key ID + secret headers (free account) | API key | none historically. A key/CAPTCHA was reported from early 2026 **(secondary)** and is consistent with the probe. | API key | none (scrapes Yahoo endpoints) |
| Rate limits | free plan: 200 requests/min | Basic (free): 5 requests/min **(secondary)** | "download limit exceeded" reports **(secondary)** | per plan | undocumented, throttled |
| History | **(unverified)** depth for SIP | Basic: 2 years, Starter 5, Developer 10 **(secondary, plan table)** | long | WIKI frozen since 2018 **(secondary)** | long |
| Intraday & timestamps | minute+ bars, RFC-3339 UTC timestamps | ms epoch timestamps (bar start) | daily only | daily | yes |
| Free tier limitations | free plan: IEX real-time; the latest 15 minutes are not queryable for historical SIP | Basic: end-of-day only; the previous day is available after midnight ET **(knowledge base)** | — | current US EOD is premium | — |
| Licensing / redistribution | subject to Alpaca market-data terms and exchange agreements. Assume **no redistribution.** | subscription terms; assume no redistribution | terms not located | per dataset licence | Yahoo terms: personal use |
| Corrections / revisions | not documented → AlphaForge versions observations itself | not documented | not documented | not documented | silent changes |
| Reproducibility | good (explicit adjustment, stable API) | good (paid tiers) | poor (access now gated) | good, paid | poor |

Sources (official unless marked): [Alpaca market data overview](https://docs.alpaca.markets/docs/about-market-data-api),
[Alpaca stock bars reference](https://docs.alpaca.markets/reference/stockbars),
[Alpaca corporate actions reference](https://docs.alpaca.markets/reference/corporateactions-1),
[Alpaca historical stock data](https://docs.alpaca.markets/docs/historical-stock-data-1),
[Massive custom bars](https://massive.com/docs/rest/stocks/aggregates/custom-bars),
[Massive knowledge base: previous-day data on Basic](https://massive.com/knowledge-base/article/on-the-free-basic-plan-when-can-i-pull-the-previous-trading-days-data),
[Nasdaq Data Link help: pricing](https://help.data.nasdaq.com/article/568-how-much-does-nasdaq-data-link-data-cost-how-do-i-find-pricing),
pandas-datareader issue on Stooq keys (secondary), and the yfinance README disclaimer (secondary).

## Decision

**Primary provider: Alpaca Market Data** (`src/finance_agent/market/providers.py:AlpacaProvider`).

- It has an explicit, documented choice of raw vs split vs dividend vs all adjustment. AlphaForge
  stores **raw** bars and derives its own adjustments from stored corporate actions, so the
  provider's adjusted close is never treated as interchangeable. Provider-adjusted bars can be
  stored separately (`provider:split`, `provider:all`) for reconciliation only.
- A corporate-actions endpoint with ex/record/payable/process dates.
- A free account with a usable rate limit (200 requests/min); `feed=sip` (consolidated) vs `iex`
  is configurable.
- Massive is the natural second adapter: its free tier (5 requests/min, 2 years) is too small for
  the universe, and paid tiers would be needed.

**Provider-independent interface:** `MarketDataProvider` (`daily_bars`, `corporate_actions`)
returns provider records tied to immutable source snapshots. Normalization, availability,
versioning, quality checks, adjustment and calculations live outside the provider.

**Status: live Alpaca integration is UNVERIFIED** — no credentials were available. The full
offline pipeline runs the *real* Alpaca adapter and parser against **synthetic** fixtures in the
Alpaca response format (`tests/fixtures/alpaca`, MANIFEST `kind: synthetic`). A live test exists
(`tests/integration/test_live.py::test_live_v03a_alpaca_market_data`) and skips without
`LIVE_ALPACA_API_KEY_ID` / `LIVE_ALPACA_API_SECRET_KEY`.

To enable live use: create a free Alpaca account, set `ALPACA_API_KEY_ID` and
`ALPACA_API_SECRET_KEY` in `.env`, then run
`uv run python -m finance_agent market ingest --start … --end …`. Things to verify on first live
use:

- whether the free plan returns `feed=sip` history, or whether `ALPACA_FEED=iex` is needed (IEX
  volume is partial);
- the depth of available history;
- the daily-bar timestamp convention (expected 00:00 New York; flagged as `WRONG_TIMEZONE`
  otherwise).

**Not used:**
- **Stooq:** access is gated and the terms are unknown.
- **Nasdaq Data Link:** paid for current US EOD.
- **yfinance:** unofficial, personal-use terms, and not reproducible. It is acceptable only as a
  manual research-only cross-check, and is not implemented.

**Never committed:** live provider responses. Raw responses live only in the local snapshot store
(`data/`, git-ignored).
