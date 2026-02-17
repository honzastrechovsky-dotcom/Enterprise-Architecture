"""Agent registry - specialist agent definitions and discovery.

Each agent is defined by an AgentSpec that declares its capabilities,
tools, system prompt, and access requirements. The registry enables
the orchestrator to discover and select the right specialist for each task.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import structlog

from src.models.user import UserRole

log = structlog.get_logger(__name__)


@dataclass
class AgentSpec:
    """Specification for a specialist agent.

    This defines everything the orchestrator needs to know about an agent:
    what it can do, what tools it uses, what access it requires, and how
    to configure LLM calls for it.
    """
    agent_id: str
    name: str
    description: str
    system_prompt: str
    capabilities: list[str]
    tools: list[str]
    required_role: UserRole = UserRole.OPERATOR
    model_preference: str | None = None
    max_tokens: int = 2048
    temperature: float = 0.7
    classification_access: list[str] = field(
        default_factory=lambda: ["class_i", "class_ii"]
    )
    requires_verification: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Validate agent spec after initialization."""
        if not self.agent_id:
            raise ValueError("agent_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        if not self.capabilities:
            raise ValueError("Agent must declare at least one capability")
        if self.temperature < 0 or self.temperature > 2:
            raise ValueError("temperature must be between 0 and 2")
        if self.max_tokens < 1:
            raise ValueError("max_tokens must be positive")


class AgentRegistry:
    """Registry for discovering and accessing specialist agents.

    This is a singleton that maintains the catalog of all available agents.
    The orchestrator queries this registry to find the right specialist
    for each task.
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentSpec] = {}
        self._default_agent_id: str | None = None

    def register(self, spec: AgentSpec) -> None:
        """Register an agent spec in the registry.

        Args:
            spec: The agent specification to register

        Raises:
            ValueError: If an agent with this ID is already registered
        """
        if spec.agent_id in self._agents:
            raise ValueError(
                f"Agent '{spec.agent_id}' is already registered. "
                "Use a unique agent_id."
            )

        self._agents[spec.agent_id] = spec
        log.info(
            "registry.agent_registered",
            agent_id=spec.agent_id,
            name=spec.name,
            capabilities=spec.capabilities,
        )

    def register_default(self, spec: AgentSpec) -> None:
        """Register an agent as both a specialist and the default fallback.

        Args:
            spec: The agent specification to register as default
        """
        self.register(spec)
        self._default_agent_id = spec.agent_id
        log.info("registry.default_agent_set", agent_id=spec.agent_id)

    def get(self, agent_id: str) -> AgentSpec | None:
        """Get an agent spec by ID.

        Args:
            agent_id: The unique identifier for the agent

        Returns:
            AgentSpec if found, None otherwise
        """
        return self._agents.get(agent_id)

    def list_agents(self, user_role: UserRole | None = None) -> list[AgentSpec]:
        """List all registered agents, optionally filtered by role access.

        Args:
            user_role: If provided, only return agents this role can access

        Returns:
            List of agent specs the user can access
        """
        if user_role is None:
            return list(self._agents.values())

        from src.core.policy import _role_level
        user_level = _role_level(user_role)

        return [
            spec for spec in self._agents.values()
            if _role_level(spec.required_role) <= user_level
        ]

    def find_by_capability(self, capability: str) -> list[AgentSpec]:
        """Find all agents that declare a specific capability.

        Args:
            capability: The capability tag to search for

        Returns:
            List of agent specs that have this capability
        """
        return [
            spec for spec in self._agents.values()
            if capability in spec.capabilities
        ]

    def get_default(self) -> AgentSpec:
        """Return the default generalist agent.

        Returns:
            The default agent spec

        Raises:
            RuntimeError: If no default agent has been registered
        """
        if self._default_agent_id is None:
            raise RuntimeError(
                "No default agent registered. Call register_default() first."
            )

        spec = self._agents.get(self._default_agent_id)
        if spec is None:
            raise RuntimeError(
                f"Default agent '{self._default_agent_id}' not found in registry"
            )

        return spec

    def clear(self) -> None:
        """Clear all registered agents. Used for testing."""
        self._agents.clear()
        self._default_agent_id = None
        log.debug("registry.cleared")


# Module-level singleton instance
_registry = AgentRegistry()


def get_registry() -> AgentRegistry:
    """Get the global agent registry instance."""
    return _registry
