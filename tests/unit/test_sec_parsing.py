"""Ticker/CIK mapping and SEC response parsing (real fixtures + synthetic edge cases)."""

from __future__ import annotations

import json

import pytest

from finance_agent.errors import MalformedDataError, UnknownTickerError
from finance_agent.providers.sec import (
    normalize_ticker,
    pad_cik,
    parse_company_tickers,
    parse_companyfacts,
    parse_submissions,
)
from finance_agent.schemas.sec import filing_index_url
from tests.conftest import FIXTURES
from tests.helpers import companyfacts_payload

SEC = FIXTURES / "sec"
EXPECTED = {
    "MSFT": "0000789019",
    "AAPL": "0000320193",
    "AMZN": "0001018724",
    "GOOGL": "0001652044",
    "META": "0001326801",
}


def _load(*parts):
    return json.loads(SEC.joinpath(*parts).read_text(encoding="utf-8"))


def test_ticker_map_resolves_initial_companies_from_real_snapshot():
    m = parse_company_tickers(_load("company_tickers.json"))
    for t, cik in EXPECTED.items():
        assert m[t].cik == cik


def test_ticker_map_handles_bad_rows_and_empty_payload():
    m = parse_company_tickers({"0": {"cik_str": 1, "ticker": "abc", "title": "X"}, "1": {"ticker": "NOCIK"}})
    assert list(m) == ["ABC"] and m["ABC"].cik == "0000000001"
    with pytest.raises(MalformedDataError):
        parse_company_tickers({"0": {"bad": 1}})
    with pytest.raises(MalformedDataError):
        parse_company_tickers([])


@pytest.mark.parametrize("bad", ["", "1ABC", "TOO-LONG-TICKER", "MS FT", "$$"])
def test_normalize_ticker_rejects_invalid(bad):
    with pytest.raises(UnknownTickerError):
        normalize_ticker(bad)


def test_pad_cik():
    assert pad_cik(789019) == "0000789019"
    with pytest.raises(MalformedDataError):
        pad_cik("12345678901")


def test_parse_submissions_real_snapshot_msft():
    profile, filings = parse_submissions(_load("submissions", "CIK0000789019.json"))
    assert profile.cik == "0000789019" and profile.fiscal_year_end == "0630"
    assert "MSFT" in profile.tickers
    assert filings and {f.form for f in filings} <= {"10-K", "10-Q", "10-K/A", "10-Q/A"}
    assert filings == sorted(filings, key=lambda f: (f.filing_date, f.accession), reverse=True)
    f = filings[0]
    assert f.index_url == filing_index_url(f.cik, f.accession)
    assert f.document_url and f.document_url.endswith(f.primary_document)


def test_parse_submissions_rejects_inconsistent_columns():
    payload = {
        "cik": "1",
        "name": "X",
        "filings": {
            "recent": {
                "accessionNumber": ["a", "b"],
                "form": ["10-K"],
                "filingDate": ["2024-01-01", "2024-01-02"],
            }
        },
    }
    with pytest.raises(MalformedDataError):
        parse_submissions(payload)
    with pytest.raises(MalformedDataError):
        parse_submissions({"name": "missing cik"})


def test_parse_companyfacts_real_snapshot_contains_expected_concepts():
    p = parse_companyfacts(_load("companyfacts", "CIK0000789019.json"))
    assert p.cik == "0000789019" and p.skipped_malformed == 0
    assert {
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "OperatingIncomeLoss",
        "PaymentsToAcquirePropertyPlantAndEquipment",
    } <= p.concepts_present
    assert all(f.form in {"10-K", "10-Q", "10-K/A", "10-Q/A"} for f in p.facts)


def test_parse_companyfacts_filters_forms_skips_malformed_and_collapses_duplicates():
    row = {
        "start": "2024-01-01",
        "end": "2024-03-31",
        "val": 10,
        "accn": "0000000001-24-000001",
        "fy": 2024,
        "fp": "Q1",
        "form": "10-Q",
        "filed": "2024-05-01",
    }
    payload = companyfacts_payload(
        {
            "Revenues": [
                row,
                dict(row),  # exact duplicate -> collapsed
                {**row, "form": "8-K"},  # non-periodic -> excluded
                {**row, "val": "not-a-number"},  # malformed value
                {k: v for k, v in row.items() if k != "end"},  # missing end
                {**row, "start": "2024-06-01"},  # start after end
            ],
            "SomeUnsupportedConcept": [row],
        }
    )
    p = parse_companyfacts(payload)
    assert len(p.facts) == 1
    assert p.skipped_nonperiodic == 1 and p.skipped_malformed == 3
    assert p.facts[0].duration_days == 91 and not p.facts[0].is_instant


def test_xbrl_fy_fp_are_preserved_as_filing_attributes():
    """fy/fp describe the filing, not the fact — they must be stored, not used for labels."""
    row = {
        "start": "2023-01-01",
        "end": "2023-03-31",
        "val": 5,
        "accn": "0000000001-24-000001",
        "fy": 2024,
        "fp": "Q1",
        "form": "10-Q",
        "filed": "2024-05-01",
    }  # prior-year comparative
    f = parse_companyfacts(companyfacts_payload({"Revenues": [row]})).facts[0]
    assert (f.fy, f.fp) == (2024, "Q1") and f.end.year == 2023
