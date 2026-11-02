# Trading-calendar methodology (v0.3-A)

Implementation: `src/finance_agent/market/calendar.py`. Stored session table:
`market_trading_sessions`.

## Source

The maintained [`exchange_calendars`](https://github.com/gerrymanoim/exchange_calendars) library
(4.13.x), calendar **XNYS** (NYSE). Nasdaq regular sessions for U.S. equities coincide with NYSE
for the instruments in scope. The library encodes:
- weekends and exchange holidays;
- special closures, e.g. the national day of mourning on 2025-01-09;
- the Saturday/Sunday holiday observance rules;
- early closes, e.g. 13:00 ET on the day after Thanksgiving.

It brings `pandas`/`numpy` as transitive dependencies. They are used only inside the calendar
wrapper; AlphaForge's own calculations remain scalar Python.

The calendar range starts 2000-01-03 and ends about one year after today (the library's
default). Dates outside the range raise `DataUnavailableError` and are never guessed.

## Session boundaries

Each session stores real UTC instants from the calendar:

| Field | Meaning |
|---|---|
| `market_open` / `market_close` | regular session (normally 09:30–16:00 ET; 13:00 close on early-close days) |
| `pre_market_open` | 04:00 ET (US extended-hours convention) |
| `after_hours_close` | regular close + 4 h (20:00 ET; 17:00 ET on early-close days) |
| `early_close` | close before 16:00 ET |

No constant UTC offset is assumed. Examples covered by tests:

| Date | Boundary in UTC | Why |
|---|---|---|
| 2026-03-06 / 2026-03-09 | open 14:30Z / 13:30Z | DST begins 2026-03-08 |
| 2026-10-30 / 2026-11-02 | close 20:00Z / 21:00Z | DST ends 2026-11-01 |
| 2025-11-28 | close 18:00Z | early close |

## Event-to-session alignment (`TradingCalendar.align`)

Input: a timezone-aware publication time (naive times are rejected). Output: phase, event
session, first reaction session and a note.

| Event time (New York) | Phase | `event_session` | `first_reaction_session` |
|---|---|---|---|
| non-session date (weekend/holiday) | `NON_TRADING_DAY` | — | next session |
| before 04:00 | `CLOSED` | — | same day |
| 04:00 – open | `PRE_MARKET` | — | same day |
| open – close (close exclusive) | `REGULAR` | same day (the daily bar is only partly after the event) | next session |
| close – after-hours close | `AFTER_HOURS` | — | **next session** |
| after after-hours close | `CLOSED` | — | next session |

An event released after the close is never assigned to that day's regular session. A release
at exactly 16:00:00 ET is after-hours. On early-close days "after the close" begins at 13:00 ET.

## Other operations

`is_session`, `sessions(start, end)`, `previous_session(d)` / `next_session(d)` (strictly
before / after), `session(d)` and `session_table(start, end)`.

## Extensibility

`TradingCalendar(name)` accepts any `exchange_calendars` code (e.g. `XLON`, `XTKS`), and
`Instrument.calendar` records each instrument's calendar. The extended-hours convention above is
U.S.-specific and would need per-market parameters before non-U.S. instruments are added.
