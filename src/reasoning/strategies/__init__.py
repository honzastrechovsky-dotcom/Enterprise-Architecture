"""Reasoning strategy implementations.

Each strategy provides a different approach to answering queries:

- ``base``: Abstract base class and ``ReasoningResult`` dataclass
- ``chain_of_thought``: Step-by-step reasoning with final verification
- ``self_consistency``: Majority-vote across N independent runs
- ``tree_of_thought``: Branch-and-prune tree search over reasoning paths
- ``rar``: Retrieval-Augmented Reasoning - interleave RAG with thinking

All strategies share the same ``reason(query, context, llm_client)`` interface
so the router and callers are decoupled from the concrete implementation.
"""

from __future__ import annotations

from src.reasoning.strategies.base import ReasoningResult, ReasoningStrategy
from src.reasoning.strategies.chain_of_thought import ChainOfThoughtStrategy
from src.reasoning.strategies.rar import RetrievalAugmentedReasoningStrategy
from src.reasoning.strategies.self_consistency import SelfConsistencyStrategy
from src.reasoning.strategies.tree_of_thought import TreeOfThoughtStrategy

__all__ = [
    "ReasoningResult",
    "ReasoningStrategy",
    "ChainOfThoughtStrategy",
    "SelfConsistencyStrategy",
    "TreeOfThoughtStrategy",
    "RetrievalAugmentedReasoningStrategy",
]
