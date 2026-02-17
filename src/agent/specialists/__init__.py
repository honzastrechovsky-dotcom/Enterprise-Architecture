"""Specialist agents package - auto-registration of all specialists.

This module imports all specialist agents and registers them with the
global agent registry on import. The registry is then available for
the orchestrator to query and select specialists.
"""

from __future__ import annotations

import structlog

from src.agent.registry import get_registry

log = structlog.get_logger(__name__)

# Import all specialist modules (this triggers SPEC definition)
from src.agent.specialists import (
    data_analyst,
    document_analyst,
    generalist,
    maintenance_advisor,
    procedure_expert,
    quality_inspector,
)

# Register all specialists with the global registry
_registry = get_registry()

# Register the default generalist agent first
_registry.register_default(generalist.SPEC)
log.info("specialists.registered", agent_id="generalist", default=True)

# Register all specialist agents
_specialists = [
    document_analyst.SPEC,
    procedure_expert.SPEC,
    data_analyst.SPEC,
    quality_inspector.SPEC,
    maintenance_advisor.SPEC,
]

for spec in _specialists:
    _registry.register(spec)
    log.info("specialists.registered", agent_id=spec.agent_id, default=False)

log.info(
    "specialists.initialization_complete",
    total_agents=len(_specialists) + 1,  # +1 for generalist
    specialist_count=len(_specialists),
)

# Export commonly used types and the registry accessor
__all__ = [
    "get_registry",
    "document_analyst",
    "procedure_expert",
    "data_analyst",
    "quality_inspector",
    "maintenance_advisor",
    "generalist",
]
