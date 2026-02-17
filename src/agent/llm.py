"""LiteLLM wrapper for model-agnostic LLM calls.

LiteLLM provides a unified interface for 100+ LLM providers. We proxy
all calls through a LiteLLM proxy server to:
1. Keep API keys out of the application code
2. Enable model routing, fallbacks, and cost tracking at the proxy level
3. Support swapping models without code changes (just config)

This module:
- Wraps litellm.completion() and litellm.aembedding()
- Handles retries with exponential backoff via tenacity
- Normalizes errors to our domain exceptions
- Logs token usage for billing/monitoring
"""

from __future__ import annotations

from typing import Any

import litellm
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from src.config import Settings, get_settings

log = structlog.get_logger(__name__)

# Types of errors worth retrying (transient network/rate-limit failures)
_RETRYABLE = (
    litellm.exceptions.RateLimitError,
    litellm.exceptions.ServiceUnavailableError,
    litellm.exceptions.Timeout,
    ConnectionError,
)


class LLMError(Exception):
    """Base exception for all LLM call failures."""


class LLMRateLimitError(LLMError):
    """Upstream LLM rate limit exceeded."""


class LLMUnavailableError(LLMError):
    """LLM service is unavailable."""


class LLMClient:
    """Thin wrapper around LiteLLM with retry logic and structured logging."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        # Configure LiteLLM to route through our proxy
        litellm.api_base = self._settings.litellm_base_url
        litellm.api_key = self._settings.litellm_api_key.get_secret_value()

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def complete(
        self,
        *,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        stream: bool = False,
        **kwargs: Any,
    ) -> litellm.ModelResponse:
        """Send a chat completion request via LiteLLM.

        Args:
            messages: List of role/content dicts (OpenAI format)
            model: Model identifier. Falls back to LITELLM_DEFAULT_MODEL.
            temperature: Sampling temperature (0.0 = deterministic)
            max_tokens: Maximum output tokens
            stream: Whether to stream the response (not yet wired to API)
            **kwargs: Additional kwargs passed to litellm.acompletion()

        Returns:
            LiteLLM ModelResponse object

        Raises:
            LLMRateLimitError: Upstream rate limit after retries
            LLMUnavailableError: Service unavailable after retries
            LLMError: Any other LLM failure
        """
        effective_model = model or self._settings.litellm_default_model

        log.debug(
            "llm.completion_request",
            model=effective_model,
            message_count=len(messages),
            max_tokens=max_tokens,
        )

        try:
            response: litellm.ModelResponse = await litellm.acompletion(
                model=effective_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                stream=stream,
                **kwargs,
            )
        except litellm.exceptions.RateLimitError as exc:
            raise LLMRateLimitError(f"Rate limit from upstream LLM: {exc}") from exc
        except litellm.exceptions.ServiceUnavailableError as exc:
            raise LLMUnavailableError(f"LLM service unavailable: {exc}") from exc
        except Exception as exc:
            raise LLMError(f"LLM completion failed: {exc}") from exc

        usage = response.usage
        if usage:
            log.info(
                "llm.completion_done",
                model=effective_model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
            )

        return response

    @retry(
        retry=retry_if_exception_type(_RETRYABLE),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def embed(
        self,
        texts: list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """Create embeddings for a list of texts.

        Returns:
            List of embedding vectors (one per input text)

        Raises:
            LLMError: If embedding fails
        """
        effective_model = model or self._settings.litellm_embedding_model

        if not texts:
            return []

        try:
            response = await litellm.aembedding(
                model=effective_model,
                input=texts,
            )
        except Exception as exc:
            raise LLMError(f"Embedding failed: {exc}") from exc

        embeddings = [item["embedding"] for item in response.data]
        log.debug(
            "llm.embedding_done",
            model=effective_model,
            text_count=len(texts),
            dimensions=len(embeddings[0]) if embeddings else 0,
        )
        return embeddings

    def extract_text(self, response: litellm.ModelResponse) -> str:
        """Extract the assistant text content from a completion response."""
        try:
            return response.choices[0].message.content or ""
        except (AttributeError, IndexError, KeyError):
            return ""

    def extract_model_name(self, response: litellm.ModelResponse) -> str:
        """Extract the model name actually used from the response."""
        try:
            return response.model or self._settings.litellm_default_model
        except AttributeError:
            return self._settings.litellm_default_model
