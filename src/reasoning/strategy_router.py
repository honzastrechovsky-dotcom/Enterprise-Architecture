"""Strategy router - selects the best reasoning strategy for a given query.

The router maps (query, complexity, task_type) to a concrete ReasoningStrategy
instance.  It supports per-agent overrides so individual agents can pin to a
specific strategy regardless of the routing defaults.

Default routing table:

    SIMPLE query                  → ChainOfThought
    SAFETY_CRITICAL / CRITICAL    → SelfConsistency (majority vote for reliability)
    PLANNING / COMPLEX            → TreeOfThought   (explore solution space)
    KNOWLEDGE_INTENSIVE           → RetrievalAugmentedReasoning

Complexity levels (string, case-insensitive):
    "low" / "simple" / "trivial"  → simple
    "medium" / "moderate"         → medium
    "high" / "complex" / "hard"   → complex

Unknown complexity defaults to "medium", which routes to ChainOfThought.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

import structlog

from src.reasoning.strategies.base import ReasoningStrategy
from src.reasoning.strategies.chain_of_thought import ChainOfThoughtStrategy
from src.reasoning.strategies.rar import RetrievalAugmentedReasoningStrategy
from src.reasoning.strategies.self_consistency import SelfConsistencyStrategy
from src.reasoning.strategies.tree_of_thought import TreeOfThoughtStrategy

log = structlog.get_logger(__name__)


class TaskType(str, Enum):
    """Supported task types for strategy routing.

    The string values are used as canonical names in logs and overrides.
    """
    SIMPLE = "simple"
    SAFETY_CRITICAL = "safety_critical"
    CRITICAL = "critical"           # Alias for SAFETY_CRITICAL
    PLANNING = "planning"
    COMPLEX = "complex"
    KNOWLEDGE_INTENSIVE = "knowledge_intensive"
    GENERAL = "general"             # Falls through to complexity-based routing


# Map complexity strings to a canonical level
_SIMPLE_COMPLEXITIES = {"low", "simple", "trivial", "easy"}
_HIGH_COMPLEXITIES = {"high", "complex", "hard", "difficult"}
# Everything else → "medium"


class StrategyRouter:
    """Selects a ReasoningStrategy based on query characteristics.

    Args:
        default_strategy:   Override the default fallback strategy name.
                            Accepted values: ``"chain_of_thought"``,
                            ``"self_consistency"``, ``"tree_of_thought"``,
                            ``"retrieval_augmented_reasoning"``.
        agent_overrides:    Per-agent strategy overrides.  Map of
                            ``{agent_id: strategy_name}``.
        cot_kwargs:         Extra kwargs forwarded to ChainOfThoughtStrategy.
        sc_kwargs:          Extra kwargs forwarded to SelfConsistencyStrategy.
        tot_kwargs:         Extra kwargs forwarded to TreeOfThoughtStrategy.
        rar_kwargs:         Extra kwargs forwarded to RAR strategy.
    """

    _STRATEGY_NAMES = {
        "chain_of_thought",
        "self_consistency",
        "tree_of_thought",
        "retrieval_augmented_reasoning",
    }

    def __init__(
        self,
        default_strategy: str = "chain_of_thought",
        agent_overrides: dict[str, str] | None = None,
        cot_kwargs: dict[str, Any] | None = None,
        sc_kwargs: dict[str, Any] | None = None,
        tot_kwargs: dict[str, Any] | None = None,
        rar_kwargs: dict[str, Any] | None = None,
    ) -> None:
        if default_strategy not in self._STRATEGY_NAMES:
            raise ValueError(
                f"Unknown default_strategy '{default_strategy}'. "
                f"Must be one of: {sorted(self._STRATEGY_NAMES)}"
            )
        self._default_strategy = default_strategy
        self._agent_overrides: dict[str, str] = agent_overrides or {}
        self._cot_kwargs = cot_kwargs or {}
        self._sc_kwargs = sc_kwargs or {}
        self._tot_kwargs = tot_kwargs or {}
        self._rar_kwargs = rar_kwargs or {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def select_strategy(
        self,
        query: str,
        complexity: str = "medium",
        task_type: TaskType | str = TaskType.GENERAL,
        agent_id: str | None = None,
    ) -> ReasoningStrategy:
        """Return the appropriate strategy for the given query context.

        Args:
            query:      The user's query (used for length heuristics).
            complexity: Complexity hint – "low"/"medium"/"high" or equivalents.
            task_type:  Semantic task type from the ``TaskType`` enum.
            agent_id:   Optional agent ID to check for per-agent overrides.

        Returns:
            An instantiated ReasoningStrategy ready to call ``.reason()``.
        """
        # 1. Per-agent override takes highest precedence
        if agent_id and agent_id in self._agent_overrides:
            override_name = self._agent_overrides[agent_id]
            log.debug(
                "strategy_router.agent_override",
                agent_id=agent_id,
                strategy=override_name,
            )
            return self._build_strategy(override_name)

        # 2. Normalise task_type to the enum
        if isinstance(task_type, str):
            try:
                task_type = TaskType(task_type.lower())
            except ValueError:
                task_type = TaskType.GENERAL

        # 3. Route by task type first (semantic override)
        strategy_name = self._route_by_task_type(task_type, complexity)

        log.info(
            "strategy_router.selected",
            query_length=len(query),
            complexity=complexity,
            task_type=task_type.value,
            strategy=strategy_name,
            agent_id=agent_id,
        )

        return self._build_strategy(strategy_name)

    def register_agent_override(self, agent_id: str, strategy_name: str) -> None:
        """Register (or update) a per-agent strategy override at runtime.

        Args:
            agent_id:      The agent ID to override.
            strategy_name: Strategy to use for this agent.

        Raises:
            ValueError: If strategy_name is not a known strategy.
        """
        if strategy_name not in self._STRATEGY_NAMES:
            raise ValueError(
                f"Unknown strategy '{strategy_name}'. "
                f"Must be one of: {sorted(self._STRATEGY_NAMES)}"
            )
        self._agent_overrides[agent_id] = strategy_name
        log.info(
            "strategy_router.override_registered",
            agent_id=agent_id,
            strategy=strategy_name,
        )

    def remove_agent_override(self, agent_id: str) -> None:
        """Remove a per-agent override so the agent uses normal routing again."""
        self._agent_overrides.pop(agent_id, None)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _route_by_task_type(self, task_type: TaskType, complexity: str) -> str:
        """Map task_type + complexity to a strategy name."""
        normalised_complexity = complexity.lower().strip()

        # Safety / critical: always SelfConsistency for reliability
        if task_type in (TaskType.SAFETY_CRITICAL, TaskType.CRITICAL):
            return "self_consistency"

        # Knowledge-intensive: RAR to fill gaps
        if task_type == TaskType.KNOWLEDGE_INTENSIVE:
            return "retrieval_augmented_reasoning"

        # Planning / complex task type: TreeOfThought
        if task_type in (TaskType.PLANNING, TaskType.COMPLEX):
            return "tree_of_thought"

        # Simple task type override
        if task_type == TaskType.SIMPLE:
            return "chain_of_thought"

        # General: fall through to complexity-based routing
        return self._route_by_complexity(normalised_complexity)

    def _route_by_complexity(self, complexity: str) -> str:
        """Map complexity level to a strategy when task_type is GENERAL."""
        if complexity in _SIMPLE_COMPLEXITIES:
            return "chain_of_thought"

        if complexity in _HIGH_COMPLEXITIES:
            return "tree_of_thought"

        # Medium / unknown → chain_of_thought (reliable, low cost)
        return self._default_strategy

    def _build_strategy(self, name: str) -> ReasoningStrategy:
        """Instantiate a strategy by name, forwarding any configured kwargs."""
        if name == "chain_of_thought":
            return ChainOfThoughtStrategy(**self._cot_kwargs)
        if name == "self_consistency":
            return SelfConsistencyStrategy(**self._sc_kwargs)
        if name == "tree_of_thought":
            return TreeOfThoughtStrategy(**self._tot_kwargs)
        if name == "retrieval_augmented_reasoning":
            return RetrievalAugmentedReasoningStrategy(**self._rar_kwargs)

        # Unreachable given validation in __init__ / register_agent_override
        raise ValueError(f"Unknown strategy name: {name}")
