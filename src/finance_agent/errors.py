"""Shared exception hierarchy."""

from __future__ import annotations


class FinanceAgentError(Exception):
    """Base class for all application errors."""


class ConfigurationError(FinanceAgentError):
    """Required configuration (e.g. a credential) is missing or invalid."""


class ProviderError(FinanceAgentError):
    """An external data provider failed."""


class ProviderHTTPError(ProviderError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class NotFoundError(ProviderError):
    """The requested entity does not exist at the provider (or in offline fixtures)."""


class RateLimitedError(ProviderHTTPError):
    """Provider kept rate-limiting after all retries."""


class MalformedDataError(ProviderError):
    """Provider returned a payload that does not match the expected structure."""


class DataUnavailableError(FinanceAgentError):
    """Valid inputs exist but required data is not available (e.g. before as_of)."""


class IntegrityError(FinanceAgentError):
    """Stored content failed an integrity (hash) check."""


class UnknownTickerError(NotFoundError):
    """Ticker does not map to an SEC filer."""
