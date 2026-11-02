"""Environment-driven configuration.

All settings come from environment variables (optionally a local ``.env`` file).
Secrets are held as ``SecretStr`` so they are never rendered by ``repr``/logging.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_FIXTURES_DIR = PROJECT_ROOT / "tests" / "fixtures"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    fred_api_key: SecretStr | None = None
    sec_user_agent: str | None = Field(
        default=None,
        description="SEC requires a descriptive User-Agent with contact info, "
        "e.g. 'FinanceAgent/0.1 your.name@example.com'.",
    )
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-opus-5-5"
    llm_enabled: bool = True

    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'data' / 'finance_agent.db').as_posix()}"
    data_dir: Path = PROJECT_ROOT / "data"
    fixtures_dir: Path = DEFAULT_FIXTURES_DIR
    log_level: str = "INFO"
    log_json: bool = True
    offline_mode: bool = False

    http_timeout_seconds: float = 30.0
    http_max_attempts: int = 4
    sec_max_requests_per_second: float = 5.0  # SEC fair-access ceiling is 10/s
    fred_max_requests_per_second: float = 2.0
    # v0.2 official event sources (robots.txt: ALFRED crawl-delay 2s, FRED 1s)
    alfred_max_requests_per_second: float = 0.5
    official_max_requests_per_second: float = 1.0  # federalreserve.gov, bls.gov, bea.gov
    cache_ttl_calendar_hours: float = 12
    # ALFRED vintages for past dates never change; today's vintage may still change.
    cache_ttl_alfred_past_hours: float = 24 * 365 * 10
    cache_ttl_alfred_today_hours: float = 1
    # Administrative ingestion over HTTP is disabled unless a token is configured.
    ingest_admin_token: SecretStr | None = None
    # Macro releases confirmed by an ALFRED vintage on the scheduled date use the official
    # scheduled release time as availability (precision SCHEDULED_TIME_CONFIRMED_DATE).
    # Set false for the strict v0.1 next-day policy.
    macro_use_confirmed_scheduled_time: bool = True

    # v0.3-A market data (Alpaca Market Data API; keys never logged or stored).
    alpaca_api_key_id: SecretStr | None = None
    alpaca_api_secret_key: SecretStr | None = None
    alpaca_data_url: str = "https://data.alpaca.markets"
    # 'sip' = consolidated tape (all US exchanges); 'iex' = IEX only (partial volume).
    alpaca_feed: str = "sip"
    alpaca_max_requests_per_second: float = 3.0  # free plan: 200 requests/minute
    cache_ttl_market_bars_hours: float = 12
    # Extreme daily move flagged for review (never deleted): fraction, e.g. 0.25 = 25 %.
    market_extreme_return_threshold: float = 0.25
    # v0.3-B event studies: wall-clock budget for one multi-event comparison (checked between events)
    event_study_time_budget_seconds: float = 120.0

    # Cache TTLs for reusing stored source snapshots instead of re-fetching.
    cache_ttl_tickers_hours: float = 24 * 7
    cache_ttl_sec_hours: float = 12
    cache_ttl_fred_hours: float = 6

    @field_validator("log_level")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @property
    def snapshots_dir(self) -> Path:
        return self.data_dir / "snapshots"

    def credential_status(self) -> dict[str, bool]:
        """Which credentials are configured — booleans only, never values."""
        return {
            "fred_api_key": bool(self.fred_api_key and self.fred_api_key.get_secret_value()),
            "sec_user_agent": bool(self.sec_user_agent),
            "anthropic_api_key": bool(self.anthropic_api_key and self.anthropic_api_key.get_secret_value()),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
