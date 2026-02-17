"""Base class and shared data types for reasoning strategies.

All concrete strategies inherit from ``ReasoningStrategy`` and return a
``ReasoningResult``.  The interface is deliberately narrow so strategies can be
swapped or composed without changing callers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.agent.llm import LLMClient


@dataclass
class ReasoningResult:
    """Complete output from a reasoning strategy invocation.

    Attributes:
        answer:         The final answer produced by the strategy.
        confidence:     Overall confidence in the answer (0.0 – 1.0).
        steps:          Ordered list of reasoning steps (human-readable strings)
                        that explain how the answer was reached.
        strategy_name:  Identifier of the strategy that produced this result.
        token_count:    Approximate total tokens consumed across all LLM calls
                        made by this strategy run. ``None`` when unavailable.
        reasoning_chain: Full structured chain – richer than ``steps`` and
                        strategy-specific.  May contain dicts or strings.
        metadata:       Optional extra data the strategy wants to surface
                        (e.g. consistency score, branch scores, source list).
    """

    answer: str
    confidence: float
    steps: list[str]
    strategy_name: str
    token_count: int | None = None
    reasoning_chain: list[Any] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class ReasoningStrategy(ABC):
    """Abstract base for all reasoning strategies.

    Subclasses must implement :meth:`reason`.  The constructor signature is
    intentionally free-form so each strategy can accept its own configuration.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this strategy (used in logs and result)."""

    @abstractmethod
    async def reason(
        self,
        query: str,
        context: str,
        llm_client: LLMClient,
    ) -> ReasoningResult:
        """Execute the reasoning strategy and return a result.

        Args:
            query:      The user's question or task description.
            context:    Any pre-retrieved context (RAG chunks, memory, etc.)
                        already available before reasoning starts.
            llm_client: Shared LLM client for all internal completions.

        Returns:
            ``ReasoningResult`` containing the answer and full trace.
        """

    # ------------------------------------------------------------------
    # Shared helpers available to all strategies
    # ------------------------------------------------------------------

    def _extract_tokens(self, response: Any) -> int:
        """Extract total token count from a LiteLLM ModelResponse.

        Returns 0 when usage metadata is not present.
        """
        try:
            usage = response.usage
            if usage:
                return int(usage.total_tokens or 0)
        except AttributeError:
            pass
        return 0
