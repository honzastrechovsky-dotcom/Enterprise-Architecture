"""Advanced reasoning strategies for the enterprise agent platform.

This package provides pluggable reasoning strategies that agents can use to
improve answer quality for different task types:

- ChainOfThought: Step-by-step reasoning with verification
- SelfConsistency: Multiple independent runs with majority voting
- TreeOfThought: Branching exploration with pruning
- RetrievalAugmentedReasoning: Interleaved RAG and reasoning

The StrategyRouter selects the appropriate strategy based on query
characteristics, task type, and configured overrides.

Usage:
    from src.reasoning import StrategyRouter, ReasoningResult
    from src.reasoning.strategies import TaskType

    router = StrategyRouter()
    strategy = router.select_strategy(
        query="What are the safety implications of X?",
        complexity="high",
        task_type=TaskType.SAFETY_CRITICAL,
    )
    result = await strategy.reason(query, context, llm_client)
"""

from __future__ import annotations

from src.reasoning.strategies.base import ReasoningResult, ReasoningStrategy
from src.reasoning.strategy_router import StrategyRouter, TaskType

__all__ = [
    "ReasoningResult",
    "ReasoningStrategy",
    "StrategyRouter",
    "TaskType",
]
