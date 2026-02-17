"""Agent composition and orchestration patterns.

Multi-agent composition patterns for complex workflows.

This module provides:
- CompositionPattern enum for different execution patterns
- Pattern executors (Pipeline, FanOut, Gate, TDDLoop)
- GoalPlanner for task decomposition and DAG execution
- AgentMemoryStore for cross-agent context persistence
"""

from __future__ import annotations

from src.agent.composition.patterns import (
    CompositionPattern,
    CompositionResult,
    FanOutExecutor,
    GateExecutor,
    PipelineExecutor,
    StageResult,
    TDDLoopExecutor,
)

__all__ = [
    "CompositionPattern",
    "CompositionResult",
    "StageResult",
    "PipelineExecutor",
    "FanOutExecutor",
    "GateExecutor",
    "TDDLoopExecutor",
]
