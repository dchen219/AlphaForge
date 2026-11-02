"""Initial v0.3-A research universe (configurable; new instruments need no schema change).

Treasury *yields* (DGS2, DGS10) are interest rates in percent, handled by
``market.yields`` — they are NOT the prices of the Treasury ETFs (SHY/IEF/TLT) listed here.
Primary exchanges are left unset: the providers' daily bars do not report them and they are
not invented.
"""

from __future__ import annotations

from finance_agent.market.schemas import AssetClass, Instrument, ProviderInstrumentMapping

_E, _F = AssetClass.EQUITY, AssetClass.ETF

DEFAULT_UNIVERSE: list[Instrument] = [
    Instrument(instrument_id="SPY", name="SPDR S&P 500 ETF Trust", asset_class=_F, group="broad_equity"),
    Instrument(instrument_id="QQQ", name="Invesco QQQ Trust", asset_class=_F, group="broad_equity"),
    Instrument(instrument_id="IWM", name="iShares Russell 2000 ETF", asset_class=_F, group="broad_equity"),
    Instrument(
        instrument_id="IWF", name="iShares Russell 1000 Growth ETF", asset_class=_F, group="growth_value"
    ),
    Instrument(
        instrument_id="IWD", name="iShares Russell 1000 Value ETF", asset_class=_F, group="growth_value"
    ),
    Instrument(
        instrument_id="SHY", name="iShares 1-3 Year Treasury Bond ETF", asset_class=_F, group="treasury_etf"
    ),
    Instrument(
        instrument_id="IEF", name="iShares 7-10 Year Treasury Bond ETF", asset_class=_F, group="treasury_etf"
    ),
    Instrument(
        instrument_id="TLT", name="iShares 20+ Year Treasury Bond ETF", asset_class=_F, group="treasury_etf"
    ),
    Instrument(
        instrument_id="XLK", name="Technology Select Sector SPDR Fund", asset_class=_F, group="sector_etf"
    ),
    Instrument(
        instrument_id="XLF", name="Financial Select Sector SPDR Fund", asset_class=_F, group="sector_etf"
    ),
    Instrument(
        instrument_id="XLE", name="Energy Select Sector SPDR Fund", asset_class=_F, group="sector_etf"
    ),
    Instrument(
        instrument_id="XLU", name="Utilities Select Sector SPDR Fund", asset_class=_F, group="sector_etf"
    ),
    Instrument(instrument_id="MSFT", name="Microsoft Corporation", asset_class=_E, group="technology"),
    Instrument(instrument_id="NVDA", name="NVIDIA Corporation", asset_class=_E, group="technology"),
    Instrument(instrument_id="GOOGL", name="Alphabet Inc. Class A", asset_class=_E, group="technology"),
    Instrument(instrument_id="AMZN", name="Amazon.com, Inc.", asset_class=_E, group="technology"),
    Instrument(instrument_id="META", name="Meta Platforms, Inc.", asset_class=_E, group="technology"),
]

# Treasury constant-maturity yields from FRED/ALFRED (Board of Governors H.15).
YIELD_SERIES: dict[str, str] = {
    "DGS2": "Market yield on U.S. Treasury securities at 2-year constant maturity, quoted on an investment basis",
    "DGS10": "Market yield on U.S. Treasury securities at 10-year constant maturity, quoted on an investment basis",
}


def default_mappings(provider: str) -> list[ProviderInstrumentMapping]:
    """Provider symbols equal the canonical tickers for the initial universe."""
    return [
        ProviderInstrumentMapping(
            instrument_id=i.instrument_id, provider=provider, provider_symbol=i.instrument_id
        )
        for i in DEFAULT_UNIVERSE
    ]
