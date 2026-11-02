# Accounting & Data Methodology (v0.1)

This document defines how raw SEC/FRED data becomes the numbers in a report. The
code is the source of truth; file references are given for each rule.

## 1. Source facts (SEC XBRL companyfacts)

`providers/sec.py: parse_companyfacts`

- Only `us-gaap` concepts listed in `research/concepts.py` are ingested.
- Only facts from periodic reports are eligible: `10-K`, `10-Q`, `10-KT`, `10-QT` and
  their `/A` amendments. Facts tagged in 8-Ks, proxies, etc. are excluded.
- Malformed rows (missing fields, non-numeric values, start after end) are counted and
  skipped — never repaired or guessed.
- Each fact keeps: concept, unit, period start/end (start is null for instants), value,
  accession, form, filing date, and the filing's own `fy`/`fp`.
- **`fy`/`fp` describe the filing, not the fact.** A FY2025 10-Q also reports FY2024
  comparatives tagged `fy=2025`. Fiscal labels are therefore derived from the period
  end date and the company's fiscal-year-end month (`FiscalCalendar`), never from `fy`/`fp`.

## 2. Information availability (`as_of`)

`timeutil.py`

EDGAR companyfacts and ALFRED expose **dates, not times**. Policy:

> A source dated *D* is usable from **D + 1 day, 00:00 America/New_York**.

EDGAR disseminates filings until 22:00 ET on the filing date (later submissions get the
next business day's filing date), and economic releases occur during the ET business day,
so this can never mark data available before it was public. Trade-off: same-day use of a
filing or release is impossible by design. Intraday `acceptanceDateTime` is stored on
filings but not used for availability in v0.1.

A bare date (`--as-of YYYY-MM-DD`, `GET ?as_of=`, or `"as_of": "YYYY-MM-DD"` in the
`POST /research/company` body) means the end of that day in New York (23:59:59 ET);
naive datetimes are interpreted as UTC. All three entry points share `parse_as_of`.

## 3. Selecting among duplicate reports of the same period

`research/normalize.py: select_by_context`

Facts are grouped by exact reporting context `(concept, unit, start, end)`. The same
context typically appears several times (original 10-Q, next year's comparative, 10-K,
amendments). Among facts **available at `as_of`**, the most recently filed wins
("latest known as of `as_of`"). If eligible values differ by more than $0.50 the value is
flagged `restated=true` and the alternatives are listed with accession and filing date.

Consequence for replay: a restatement or amendment filed after `as_of` is invisible,
and a later filing's comparative figures are never used for an earlier `as_of`.

## 4. Period classification and standalone quarters

`classify_duration` uses inclusive day counts: quarter 84–100, half-year 175–190,
nine-month 266–282, annual 357–374 (covers 52/53-week calendars).

Standalone quarter values:

1. **Reported**: a ~3-month duration fact for the period.
2. **Derived** (`derived_ytd_difference`) only when *all* hold:
   - cumulative fact A (6M, 9M or FY) and cumulative fact B one step shorter (3M, 6M, 9M);
   - A and B have the **same start date** (same fiscal year-to-date);
   - B ends 80–100 days before A ends (exactly one quarter);
   - exactly one such B exists; same concept and unit.

   Quarter = A − B, with period B.end+1 … A.end and both source facts recorded.
3. Otherwise **no value** — e.g. Q4 revenue is *not* computed as FY − (Q1+Q2+Q3) when no
   9M YTD fact exists, and YTD facts with different start dates are never subtracted.

If a reported quarter and a valid derivation disagree, the reported value is used and a
warning recorded. Typical real-world need: Microsoft does not tag Q4 standalone values
(Q4 = FY − 9M), and Meta/Alphabet cash-flow statements are YTD-only in 10-Qs.

Instant (balance-sheet) values are matched to the quarter-end date exactly.

## 5. Concept mapping

Concepts are tried in priority order **per period**, because filers change tags (Alphabet
moved revenue from `RevenueFromContractWithCustomerExcludingAssessedTax` to `Revenues` in
2025). The concept used is stored on every value; reports list metrics whose concept
changes across periods.

| Metric | Concepts (priority order) |
|---|---|
| Revenue | RevenueFromContractWithCustomerExcludingAssessedTax, Revenues, RevenueFromContractWithCustomerIncludingAssessedTax, SalesRevenueNet |
| Gross profit | GrossProfit |
| Cost of revenue | CostOfRevenue, CostOfGoodsAndServicesSold |
| Operating income | OperatingIncomeLoss |
| Net income | NetIncomeLoss |
| Operating cash flow | NetCashProvidedByUsedInOperatingActivities |
| CAPEX | PaymentsToAcquirePropertyPlantAndEquipment, PaymentsToAcquireProductiveAssets |
| Cash | CashAndCashEquivalentsAtCarryingValue, CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents |
| Debt components | LongTermDebt; LongTermDebtNoncurrent; LongTermDebtCurrent; CommercialPaper, ShortTermBorrowings |

## 6. CAPEX definition

**CAPEX = cash purchases of property, plant and equipment** from the investing section
of the cash-flow statement (`PaymentsToAcquirePropertyPlantAndEquipment`), gross of
disposal proceeds.

- Fallback `PaymentsToAcquireProductiveAssets` (e.g. Amazon's "Purchases of property and
  equipment") is used only when the primary tag is absent, and every such value carries a
  warning because the tag may include other productive assets.
- **Excluded**: principal repayments of finance leases, assets acquired under finance
  leases or build-to-suit arrangements (non-cash), business acquisitions, purchases of
  intangible assets, and proceeds from asset sales.
- Free cash flow = operating cash flow − CAPEX. Because finance-lease payments are
  excluded, FCF is higher than "FCF less finance leases" measures some companies publish.

## 7. Capital structure

`Normalizer.total_debt`

Total debt (excluding operating and finance lease liabilities) at a quarter-end date:

1. `LongTermDebtNoncurrent` + `LongTermDebtCurrent` — balance-sheet **carrying amounts**
   (preferred: one measurement basis across periods); else
2. `LongTermDebt` — includes the current portion, but many filers tag it at
   **face/principal** value in the debt footnote (2026-11-02 audit: AMZN differs from
   the carrying components at 31/31 dates, by up to $0.9B; AAPL 17/20; GOOGL 4/4), so it is
   a fallback only and every such value carries a warning; else
3. `LongTermDebtNoncurrent` alone — current portion untagged (warning);
4. plus `CommercialPaper` (else `ShortTermBorrowings`) when tagged for that date.

`DebtCurrent` is not used: it includes short-term borrowings/commercial paper and would
double count with step 4. `LongTermDebt` is never added to its own components. Net debt = total debt − cash and
cash equivalents; marketable securities are **not** netted, so net debt overstates
leverage for companies with large investment portfolios (warned on every value).

## 8. Calculations

`research/calculations.py` — each returns value, unit, formula, inputs (with evidence
ids), warnings and status. A result is `unavailable` (value `null`) when an input is
missing, units differ, periods differ, or a denominator is zero.

| Calculation | Formula | Comparability checks |
|---|---|---|
| Revenue YoY | (rev − rev_prior_year) / \|rev_prior_year\| | same fiscal quarter, prior fiscal year; durations within 7 days |
| Revenue QoQ | (rev − rev_prior_quarter) / \|rev_prior_quarter\| | immediately preceding fiscal quarter; seasonality warning |
| Gross margin | gross_profit / revenue | fallback (revenue − cost_of_revenue)/revenue, flagged |
| Operating / net margin | operating_income / revenue, net_income / revenue | identical period |
| FCF, FCF margin | ocf − capex; fcf / revenue | identical period |
| CAPEX / revenue, CAPEX YoY | capex / revenue; YoY as above | |
| OCF / CAPEX | ocf / capex (multiple) | |
| Net debt | total_debt − cash | identical instant date |

Negative bases are computed against |prior| and flagged.

## 9. Macro data (FRED/ALFRED)

`providers/fred.py`

- Kept separately: observation date (period measured), `realtime_start`/`realtime_end`
  (vintage window), `publication_time` (always `null` — FRED gives no intraday time),
  `available_at` (policy above) and `retrieved_at`.
- Live `as_of` requests ask ALFRED for the real-time date D = (as_of in ET) − 1 day,
  i.e. the latest vintage usable under the policy. ALFRED reports the real-time window
  clipped to the request, so in live mode `vintage_date` is the effective vintage, not
  the first-release date of each value.
- `select_vintage` applies the same rule locally to full vintage histories (fixtures,
  replay tests). Missing values (`"."`) become `null` with `missing=true`.

## 10. Verification

`verification/checks.py` runs on every report:

| Check | Fails when |
|---|---|
| evidence_references | a finding cites no evidence or an unknown id |
| numeric_claims | a number in a finding does not match its cited evidence at the displayed precision (unitless integers count unless they are years or explicit counts such as "8 quarters"; direction words are not checked) |
| arithmetic_consistency | an independent recomputation of any calculation or derived fact disagrees |
| provenance_complete | a value lacks accession, filing date, concept, unit, period or URL |
| as_of_compliance | any source became available after `as_of` |
| data_availability | (warning) requested metrics are missing for in-scope quarters |
| llm_output_screening | (warning) LLM statements were dropped as unsupported |

LLM interpretations are screened with the same numeric-claim checker; unsupported items
are removed before the report is saved. The LLM's executive summary must contain no figures.
