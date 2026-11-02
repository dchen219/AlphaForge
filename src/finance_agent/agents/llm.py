"""Pluggable LLM provider interface.

The LLM is used only for request interpretation and narrative synthesis. It never
supplies numbers: its output is checked against the evidence registry and any
unsupported claim is dropped.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from finance_agent.config.logging import redact
from finance_agent.errors import FinanceAgentError

log = logging.getLogger(__name__)


class LLMUnavailableError(FinanceAgentError):
    pass


class LLMProvider(Protocol):
    name: str

    @property
    def available(self) -> bool: ...

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 16000
    ) -> dict[str, Any]: ...


class NullLLM:
    """Used when no API key is configured or LLM use is disabled."""

    name = "none"

    def __init__(self, reason: str = "LLM disabled") -> None:
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    def complete_json(self, **_: Any) -> dict[str, Any]:
        raise LLMUnavailableError(self.reason)


class AnthropicLLM:
    """Claude via the official Anthropic SDK with structured JSON output.

    Uses adaptive thinking, an explicit effort level, and server-side refusal
    fallbacks (``fallbacks="default"``) so a classifier decline is retried on
    Anthropic's recommended fallback model instead of failing the run.
    """

    FALLBACK_BETA = "server-side-fallback-2026-07-01"

    def __init__(
        self, api_key: str, model: str = "claude-opus-5-5", effort: str = "medium", timeout: float = 180.0
    ) -> None:
        import anthropic

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=2)
        self.model = model
        self.effort = effort
        self.name = f"anthropic:{model}"

    @property
    def available(self) -> bool:
        return True

    def complete_json(
        self, *, system: str, user: str, schema: dict[str, Any], max_tokens: int = 16000
    ) -> dict[str, Any]:
        a = self._anthropic
        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                betas=[self.FALLBACK_BETA],
                fallbacks="default",
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
                system=system,
                messages=[{"role": "user", "content": user}],
            )
        except a.AuthenticationError as exc:
            raise LLMUnavailableError("Anthropic authentication failed (check ANTHROPIC_API_KEY).") from exc
        except a.RateLimitError as exc:
            raise LLMUnavailableError("Anthropic rate limit exceeded.") from exc
        except a.APIStatusError as exc:
            raise LLMUnavailableError(
                f"Anthropic API error {exc.status_code}: {redact(str(exc))[:300]}"
            ) from exc
        except a.APIConnectionError as exc:
            raise LLMUnavailableError("Could not reach the Anthropic API.") from exc
        if resp.stop_reason == "refusal":
            raise LLMUnavailableError("Model declined the request (refusal).")
        if resp.stop_reason == "max_tokens":
            raise LLMUnavailableError("Model output truncated (max_tokens).")
        text = next((b.text for b in resp.content if getattr(b, "type", None) == "text"), None)
        if text is None:
            raise LLMUnavailableError("Model returned no text content.")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise LLMUnavailableError("Model returned invalid JSON.") from exc


def build_llm(api_key: str | None, model: str, enabled: bool = True) -> LLMProvider:
    if not enabled:
        return NullLLM("LLM synthesis disabled by configuration/request.")
    if not api_key:
        return NullLLM("ANTHROPIC_API_KEY not configured.")
    try:
        return AnthropicLLM(api_key=api_key, model=model)
    except Exception as exc:  # pragma: no cover - import/config failure
        log.warning("could not initialise Anthropic client", extra={"error": type(exc).__name__})
        return NullLLM(f"Anthropic client unavailable: {type(exc).__name__}")
