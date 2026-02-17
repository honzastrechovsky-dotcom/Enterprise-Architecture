"""Fallback chain for resilient model execution with automatic retries.

The FallbackChain executes LLM requests with automatic fallback to alternative
tiers when failures occur. This provides resilience against:
- Model unavailability
- Timeout errors
- Rate limiting
- Low-confidence responses

Fallback strategy:
1. Try preferred tier
2. On failure, try next tier in chain
3. On failure, try next tier
4. If all fail, return safe error (AP.7: fail secure)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import litellm
import structlog

if TYPE_CHECKING:
    from src.agent.llm import LLMClient
    from src.agent.model_router.router import ModelConfig

log = structlog.get_logger(__name__)


class FallbackChain:
    """Executes LLM requests with automatic fallback through model tiers.

    The chain tries models in order until one succeeds or all fail.
    Each failure is logged for metrics and observability.
    """

    def __init__(self, tiers: list[ModelConfig]) -> None:
        """Initialize fallback chain with ordered tiers.

        Args:
            tiers: List of ModelConfig in priority order (preferred first)
        """
        if not tiers:
            raise ValueError("FallbackChain requires at least one tier")

        self._tiers = tiers
        self._fallback_events: list[dict[str, Any]] = []

        log.info(
            "fallback_chain.initialized",
            tier_count=len(tiers),
            tiers=[tier.model_id for tier in tiers],
        )

    async def execute_with_fallback(
        self,
        llm_client: LLMClient,
        messages: list[dict[str, str]],
        preferred_tier: ModelConfig,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> tuple[litellm.ModelResponse, ModelConfig]:
        """Execute LLM request with automatic fallback on failure.

        Tries tiers in order:
        1. Preferred tier
        2. Next tier in chain (if preferred fails)
        3. Continue until success or exhaustion

        Args:
            llm_client: LLMClient instance for making requests
            messages: Chat messages in OpenAI format
            preferred_tier: Preferred ModelConfig to try first
            temperature: Sampling temperature
            max_tokens: Maximum output tokens (uses tier default if None)
            **kwargs: Additional kwargs for LLM completion

        Returns:
            Tuple of (response, actual_tier_used)

        Raises:
            RuntimeError: If all tiers fail (fail-secure)
        """
        # Build execution order: preferred first, then rest of chain
        execution_order = [preferred_tier]
        for tier in self._tiers:
            if tier.model_id != preferred_tier.model_id:
                execution_order.append(tier)

        last_error: Exception | None = None

        for tier in execution_order:
            try:
                log.info(
                    "fallback_chain.attempting_tier",
                    model_id=tier.model_id,
                    tier=tier.tier.value,
                )

                # Use tier's max_tokens if not overridden
                effective_max_tokens = max_tokens or tier.max_tokens

                response = await llm_client.complete(
                    messages=messages,
                    model=tier.model_id,
                    temperature=temperature,
                    max_tokens=effective_max_tokens,
                    **kwargs,
                )

                # Success - log and return
                log.info(
                    "fallback_chain.tier_succeeded",
                    model_id=tier.model_id,
                    tier=tier.tier.value,
                    preferred_tier=preferred_tier.tier.value,
                    fallback_occurred=(tier.model_id != preferred_tier.model_id),
                )

                return response, tier

            except Exception as exc:
                # Log failure and try next tier
                last_error = exc

                self._fallback_events.append(
                    {
                        "tier": tier.tier.value,
                        "model_id": tier.model_id,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )

                log.warning(
                    "fallback_chain.tier_failed",
                    model_id=tier.model_id,
                    tier=tier.tier.value,
                    error_type=type(exc).__name__,
                    error_message=str(exc),
                    remaining_tiers=len(execution_order)
                    - execution_order.index(tier)
                    - 1,
                )

                # Continue to next tier
                continue

        # All tiers exhausted - fail secure (AP.7)
        log.error(
            "fallback_chain.all_tiers_failed",
            preferred_tier=preferred_tier.tier.value,
            attempted_tiers=[tier.tier.value for tier in execution_order],
            fallback_events=self._fallback_events,
        )

        raise RuntimeError(
            f"All model tiers failed. Last error: {last_error}. "
            "This is a system-level failure requiring investigation."
        )

    def get_fallback_events(self) -> list[dict[str, Any]]:
        """Get list of fallback events that occurred.

        Returns:
            List of fallback event dicts with tier, error info
        """
        return self._fallback_events.copy()

    def reset_events(self) -> None:
        """Clear fallback event history. Used for testing."""
        self._fallback_events.clear()
        log.debug("fallback_chain.events_reset")
