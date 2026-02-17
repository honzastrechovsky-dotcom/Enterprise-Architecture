"""Model router - intelligent model tier selection and escalation.

The router selects the appropriate model tier (LIGHT/STANDARD/HEAVY) based on:
- Task complexity score from ComplexityEstimator
- Agent declared preferences
- Model availability
- Budget constraints (future)

Default model mapping:
- LIGHT: ollama/qwen2.5:7b — intent classification, PII, simple Q&A
- STANDARD: ollama/qwen2.5:32b — agent execution, skill invocation
- HEAVY: vllm/qwen2.5:72b — thinking tools, security analysis, architecture
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from src.agent.registry import AgentSpec
    from src.config import Settings

log = structlog.get_logger(__name__)


class ModelTier(str, Enum):
    """Model performance tiers for routing decisions."""

    LIGHT = "light"  # Fast, cheap models for simple tasks
    STANDARD = "standard"  # Balanced performance/cost for most tasks
    HEAVY = "heavy"  # Premium models for complex reasoning


@dataclass
class ModelConfig:
    """Configuration for a specific model at a tier.

    Attributes:
        tier: Performance tier this model belongs to
        model_id: LiteLLM model identifier (e.g., "ollama/qwen2.5:7b")
        max_tokens: Maximum output tokens for this model
        cost_weight: Relative cost factor (1.0 = baseline, higher = more expensive)
        gpu_memory_gb: Estimated GPU memory required (for capacity planning)
    """

    tier: ModelTier
    model_id: str
    max_tokens: int
    cost_weight: float
    gpu_memory_gb: float

    def __post_init__(self) -> None:
        """Validate model config after initialization."""
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")
        if self.cost_weight <= 0:
            raise ValueError("cost_weight must be positive")
        if self.gpu_memory_gb < 0:
            raise ValueError("gpu_memory_gb cannot be negative")


class ModelRouter:
    """Intelligent model tier selection and escalation.

    The router maintains a catalog of available models and routes requests
    to the most appropriate tier based on task complexity, agent preferences,
    and system constraints.
    """

    def __init__(
        self,
        settings: Settings,
        available_models: list[ModelConfig] | None = None,
    ) -> None:
        """Initialize model router with configuration.

        Args:
            settings: Application settings with model identifiers
            available_models: Optional list of ModelConfig. If None, uses defaults.
        """
        self._settings = settings

        # Default model catalog if not provided
        if available_models is None:
            available_models = [
                ModelConfig(
                    tier=ModelTier.LIGHT,
                    model_id=settings.model_light,
                    max_tokens=2048,
                    cost_weight=1.0,
                    gpu_memory_gb=8.0,
                ),
                ModelConfig(
                    tier=ModelTier.STANDARD,
                    model_id=settings.model_standard,
                    max_tokens=4096,
                    cost_weight=3.0,
                    gpu_memory_gb=32.0,
                ),
                ModelConfig(
                    tier=ModelTier.HEAVY,
                    model_id=settings.model_heavy,
                    max_tokens=8192,
                    cost_weight=10.0,
                    gpu_memory_gb=72.0,
                ),
            ]

        self._models = {model.tier: model for model in available_models}
        log.info(
            "model_router.initialized",
            tiers=[tier.value for tier in self._models.keys()],
            models={tier.value: model.model_id for tier, model in self._models.items()},
        )

    def route(
        self,
        task_type: str,
        complexity_score: float,
        agent_spec: AgentSpec | None = None,
    ) -> ModelConfig:
        """Select best model based on task type, complexity, and agent preference.

        Selection priority:
        1. Agent's declared model_preference (if specified)
        2. Complexity score mapping (0-0.3=LIGHT, 0.3-0.7=STANDARD, 0.7-1.0=HEAVY)
        3. Task type heuristics
        4. Default to STANDARD

        Args:
            task_type: Type of task ("intent_classification", "agent_execution", etc.)
            complexity_score: Complexity score from ComplexityEstimator (0.0-1.0)
            agent_spec: Optional agent spec with model preference

        Returns:
            ModelConfig for the selected tier
        """
        # Priority 1: Respect agent's declared preference
        if agent_spec and agent_spec.model_preference:
            # Find which tier contains this model
            for tier, model in self._models.items():
                if model.model_id == agent_spec.model_preference:
                    log.debug(
                        "model_router.agent_preference",
                        agent_id=agent_spec.agent_id,
                        model=model.model_id,
                        tier=tier.value,
                    )
                    return model
            # If preference not found in our catalog, log warning and continue
            log.warning(
                "model_router.preference_not_found",
                agent_id=agent_spec.agent_id,
                preference=agent_spec.model_preference,
            )

        # Priority 2: Complexity score mapping
        if complexity_score < 0.3:
            selected_tier = ModelTier.LIGHT
        elif complexity_score < 0.7:
            selected_tier = ModelTier.STANDARD
        else:
            selected_tier = ModelTier.HEAVY

        # Priority 3: Task type overrides
        # Certain task types always use specific tiers
        task_tier_map = {
            "intent_classification": ModelTier.LIGHT,
            "pii_detection": ModelTier.LIGHT,
            "simple_qa": ModelTier.LIGHT,
            "thinking_tools": ModelTier.HEAVY,
            "security_analysis": ModelTier.HEAVY,
            "architecture": ModelTier.HEAVY,
        }

        if task_type in task_tier_map:
            selected_tier = task_tier_map[task_type]
            log.debug(
                "model_router.task_type_override",
                task_type=task_type,
                tier=selected_tier.value,
            )

        model = self._models[selected_tier]

        log.info(
            "model_router.route_selected",
            task_type=task_type,
            complexity_score=complexity_score,
            selected_tier=selected_tier.value,
            model_id=model.model_id,
        )

        return model

    def escalate(self, current_tier: ModelTier) -> ModelConfig:
        """Escalate to next higher tier when current fails or shows low confidence.

        Escalation path: LIGHT → STANDARD → HEAVY → (error)

        Args:
            current_tier: The tier that failed or needs escalation

        Returns:
            ModelConfig for the next higher tier

        Raises:
            ValueError: If already at highest tier (HEAVY)
        """
        if current_tier == ModelTier.LIGHT:
            next_tier = ModelTier.STANDARD
        elif current_tier == ModelTier.STANDARD:
            next_tier = ModelTier.HEAVY
        else:
            log.error(
                "model_router.escalation_failed",
                current_tier=current_tier.value,
                reason="already_at_highest_tier",
            )
            raise ValueError(
                f"Cannot escalate from {current_tier.value} - already at highest tier"
            )

        model = self._models[next_tier]
        log.info(
            "model_router.escalated",
            from_tier=current_tier.value,
            to_tier=next_tier.value,
            model_id=model.model_id,
        )

        return model

    def fallback(self, current_tier: ModelTier) -> ModelConfig:
        """Fall back to cheaper tier for retry after failure.

        Fallback path: HEAVY → STANDARD → LIGHT → (error)

        This is used when a model fails and we want to retry with a
        simpler/cheaper model, as opposed to escalate which moves up.

        Args:
            current_tier: The tier that failed

        Returns:
            ModelConfig for the next lower tier

        Raises:
            ValueError: If already at lowest tier (LIGHT)
        """
        if current_tier == ModelTier.HEAVY:
            next_tier = ModelTier.STANDARD
        elif current_tier == ModelTier.STANDARD:
            next_tier = ModelTier.LIGHT
        else:
            log.error(
                "model_router.fallback_failed",
                current_tier=current_tier.value,
                reason="already_at_lowest_tier",
            )
            raise ValueError(
                f"Cannot fall back from {current_tier.value} - already at lowest tier"
            )

        model = self._models[next_tier]
        log.info(
            "model_router.fallback",
            from_tier=current_tier.value,
            to_tier=next_tier.value,
            model_id=model.model_id,
        )

        return model

    def get_model_for_agent(self, agent_spec: AgentSpec) -> ModelConfig:
        """Get model configuration respecting agent's declared preference.

        If agent declares a model_preference, use that. Otherwise default
        to STANDARD tier.

        Args:
            agent_spec: Agent specification with optional model preference

        Returns:
            ModelConfig for the agent
        """
        if agent_spec.model_preference:
            # Try to find exact match in catalog
            for model in self._models.values():
                if model.model_id == agent_spec.model_preference:
                    log.debug(
                        "model_router.agent_model_matched",
                        agent_id=agent_spec.agent_id,
                        model_id=model.model_id,
                    )
                    return model

            # If not in catalog, log warning and fall back to STANDARD
            log.warning(
                "model_router.agent_preference_unknown",
                agent_id=agent_spec.agent_id,
                preference=agent_spec.model_preference,
                fallback="STANDARD",
            )

        # Default to STANDARD tier
        model = self._models[ModelTier.STANDARD]
        log.debug(
            "model_router.agent_model_default",
            agent_id=agent_spec.agent_id,
            model_id=model.model_id,
        )
        return model

    def get_tier_by_model_id(self, model_id: str) -> ModelTier | None:
        """Look up which tier a model ID belongs to.

        Args:
            model_id: LiteLLM model identifier

        Returns:
            ModelTier if found, None otherwise
        """
        for tier, model in self._models.items():
            if model.model_id == model_id:
                return tier
        return None
