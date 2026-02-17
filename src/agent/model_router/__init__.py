"""Model routing and token economy for intelligent LLM selection.

This module provides intelligent model selection based on task complexity,
token budget management, and automatic fallback chains. It integrates with
LiteLLM to route requests to appropriate model tiers (LIGHT/STANDARD/HEAVY)
based on:
- Task complexity estimation
- Agent requirements
- Token budget constraints
- Performance metrics

BudgetManager and ModelMetricsCollector use in-memory storage by default.
Use the Persistent* variants for durable, multi-instance deployments.
"""

from __future__ import annotations

from src.agent.model_router.budget import BudgetManager, TokenBudget
from src.agent.model_router.complexity import ComplexityEstimator, TaskComplexity
from src.agent.model_router.fallback import FallbackChain
from src.agent.model_router.metrics import ModelMetricsCollector, RoutingDecision
from src.agent.model_router.router import ModelConfig, ModelRouter, ModelTier

__all__ = [
    "BudgetManager",
    "ComplexityEstimator",
    "FallbackChain",
    "ModelConfig",
    "ModelMetricsCollector",
    "ModelRouter",
    "ModelTier",
    "RoutingDecision",
    "TaskComplexity",
    "TokenBudget",
]
