"""Tests for AgentRegistry and AgentSpec.

Tests cover:
- AgentSpec creation and validation
- AgentRegistry.register() and get()
- Registry returns correct specialist for intent
- Registry lists all registered agents
- Duplicate registration handling
"""

from __future__ import annotations

import pytest

from src.agent.registry import AgentRegistry, AgentSpec, get_registry
from src.models.user import UserRole


# ------------------------------------------------------------------ #
# AgentSpec validation tests
# ------------------------------------------------------------------ #


def test_agent_spec_creation_success():
    """Test AgentSpec creation with valid fields."""
    spec = AgentSpec(
        agent_id="test_agent",
        name="Test Agent",
        description="A test agent for unit tests",
        system_prompt="You are a test agent.",
        capabilities=["testing", "validation"],
        tools=["test_tool"],
        required_role=UserRole.OPERATOR,
        model_preference="ollama/qwen2.5:7b",
        max_tokens=2048,
        temperature=0.7,
    )

    assert spec.agent_id == "test_agent"
    assert spec.name == "Test Agent"
    assert spec.capabilities == ["testing", "validation"]
    assert spec.required_role == UserRole.OPERATOR
    assert spec.temperature == 0.7


def test_agent_spec_empty_agent_id_fails():
    """Test that AgentSpec validation fails with empty agent_id."""
    with pytest.raises(ValueError, match="agent_id cannot be empty"):
        AgentSpec(
            agent_id="",
            name="Test Agent",
            description="A test agent",
            system_prompt="You are a test agent.",
            capabilities=["testing"],
            tools=[],
        )


def test_agent_spec_empty_name_fails():
    """Test that AgentSpec validation fails with empty name."""
    with pytest.raises(ValueError, match="name cannot be empty"):
        AgentSpec(
            agent_id="test_agent",
            name="",
            description="A test agent",
            system_prompt="You are a test agent.",
            capabilities=["testing"],
            tools=[],
        )


def test_agent_spec_no_capabilities_fails():
    """Test that AgentSpec validation fails with no capabilities."""
    with pytest.raises(ValueError, match="must declare at least one capability"):
        AgentSpec(
            agent_id="test_agent",
            name="Test Agent",
            description="A test agent",
            system_prompt="You are a test agent.",
            capabilities=[],
            tools=[],
        )


def test_agent_spec_invalid_temperature_fails():
    """Test that AgentSpec validation fails with invalid temperature."""
    with pytest.raises(ValueError, match="temperature must be between 0 and 2"):
        AgentSpec(
            agent_id="test_agent",
            name="Test Agent",
            description="A test agent",
            system_prompt="You are a test agent.",
            capabilities=["testing"],
            tools=[],
            temperature=3.0,
        )


def test_agent_spec_invalid_max_tokens_fails():
    """Test that AgentSpec validation fails with invalid max_tokens."""
    with pytest.raises(ValueError, match="max_tokens must be positive"):
        AgentSpec(
            agent_id="test_agent",
            name="Test Agent",
            description="A test agent",
            system_prompt="You are a test agent.",
            capabilities=["testing"],
            tools=[],
            max_tokens=0,
        )


def test_agent_spec_defaults():
    """Test that AgentSpec applies correct defaults."""
    spec = AgentSpec(
        agent_id="test_agent",
        name="Test Agent",
        description="A test agent",
        system_prompt="You are a test agent.",
        capabilities=["testing"],
        tools=[],
    )

    assert spec.required_role == UserRole.OPERATOR  # Default
    assert spec.model_preference is None  # Default
    assert spec.max_tokens == 2048  # Default
    assert spec.temperature == 0.7  # Default
    assert spec.classification_access == ["class_i", "class_ii"]  # Default
    assert spec.requires_verification is True  # Default


# ------------------------------------------------------------------ #
# AgentRegistry tests
# ------------------------------------------------------------------ #


@pytest.fixture
def registry():
    """Create a fresh registry for each test."""
    reg = AgentRegistry()
    return reg


@pytest.fixture
def sample_spec():
    """Create a sample AgentSpec for testing."""
    return AgentSpec(
        agent_id="test_agent",
        name="Test Agent",
        description="A test agent for unit tests",
        system_prompt="You are a test agent.",
        capabilities=["testing", "validation"],
        tools=["test_tool"],
    )


def test_registry_register_success(registry: AgentRegistry, sample_spec: AgentSpec):
    """Test successful agent registration."""
    registry.register(sample_spec)

    retrieved = registry.get("test_agent")
    assert retrieved is not None
    assert retrieved.agent_id == "test_agent"
    assert retrieved.name == "Test Agent"


def test_registry_duplicate_registration_fails(
    registry: AgentRegistry, sample_spec: AgentSpec
):
    """Test that duplicate registration raises ValueError."""
    registry.register(sample_spec)

    with pytest.raises(ValueError, match="is already registered"):
        registry.register(sample_spec)


def test_registry_get_nonexistent_returns_none(registry: AgentRegistry):
    """Test that get() returns None for nonexistent agent."""
    result = registry.get("nonexistent_agent")
    assert result is None


def test_registry_register_default(registry: AgentRegistry, sample_spec: AgentSpec):
    """Test registering an agent as default."""
    registry.register_default(sample_spec)

    # Should be retrievable as regular agent
    retrieved = registry.get("test_agent")
    assert retrieved is not None

    # Should be retrievable as default
    default = registry.get_default()
    assert default.agent_id == "test_agent"


def test_registry_get_default_before_registration_fails(registry: AgentRegistry):
    """Test that get_default() fails if no default registered."""
    with pytest.raises(RuntimeError, match="No default agent registered"):
        registry.get_default()


def test_registry_list_agents_empty(registry: AgentRegistry):
    """Test list_agents() returns empty list for empty registry."""
    agents = registry.list_agents()
    assert agents == []


def test_registry_list_agents_returns_all(registry: AgentRegistry):
    """Test list_agents() returns all registered agents."""
    spec1 = AgentSpec(
        agent_id="agent1",
        name="Agent 1",
        description="First agent",
        system_prompt="Prompt 1",
        capabilities=["cap1"],
        tools=[],
    )
    spec2 = AgentSpec(
        agent_id="agent2",
        name="Agent 2",
        description="Second agent",
        system_prompt="Prompt 2",
        capabilities=["cap2"],
        tools=[],
    )

    registry.register(spec1)
    registry.register(spec2)

    agents = registry.list_agents()
    assert len(agents) == 2
    agent_ids = {agent.agent_id for agent in agents}
    assert agent_ids == {"agent1", "agent2"}


def test_registry_list_agents_filtered_by_role(registry: AgentRegistry):
    """Test list_agents() filters by user role."""
    spec_operator = AgentSpec(
        agent_id="operator_agent",
        name="Operator Agent",
        description="For operators",
        system_prompt="Prompt",
        capabilities=["operator_cap"],
        tools=[],
        required_role=UserRole.OPERATOR,
    )
    spec_admin_only = AgentSpec(
        agent_id="admin_only_agent",
        name="Admin Only Agent",
        description="For admins only",
        system_prompt="Prompt",
        capabilities=["admin_only_cap"],
        tools=[],
        required_role=UserRole.ADMIN,
    )
    spec_admin = AgentSpec(
        agent_id="admin_agent",
        name="Admin Agent",
        description="For admins",
        system_prompt="Prompt",
        capabilities=["admin_cap"],
        tools=[],
        required_role=UserRole.ADMIN,
    )

    registry.register(spec_operator)
    registry.register(spec_admin_only)
    registry.register(spec_admin)

    # VIEWER should see no agents (below OPERATOR)
    viewer_agents = registry.list_agents(UserRole.VIEWER)
    assert len(viewer_agents) == 0  # VIEWER < OPERATOR in hierarchy

    # OPERATOR should see only OPERATOR agents
    operator_agents = registry.list_agents(UserRole.OPERATOR)
    assert len(operator_agents) == 1
    assert operator_agents[0].agent_id == "operator_agent"

    # ADMIN should see all agents
    admin_agents = registry.list_agents(UserRole.ADMIN)
    assert len(admin_agents) == 3


def test_registry_find_by_capability_single_match(registry: AgentRegistry):
    """Test find_by_capability() returns single matching agent."""
    spec = AgentSpec(
        agent_id="test_agent",
        name="Test Agent",
        description="A test agent",
        system_prompt="Prompt",
        capabilities=["testing", "validation"],
        tools=[],
    )
    registry.register(spec)

    results = registry.find_by_capability("testing")
    assert len(results) == 1
    assert results[0].agent_id == "test_agent"


def test_registry_find_by_capability_multiple_matches(registry: AgentRegistry):
    """Test find_by_capability() returns multiple matching agents."""
    spec1 = AgentSpec(
        agent_id="agent1",
        name="Agent 1",
        description="First",
        system_prompt="Prompt",
        capabilities=["testing", "cap1"],
        tools=[],
    )
    spec2 = AgentSpec(
        agent_id="agent2",
        name="Agent 2",
        description="Second",
        system_prompt="Prompt",
        capabilities=["testing", "cap2"],
        tools=[],
    )
    registry.register(spec1)
    registry.register(spec2)

    results = registry.find_by_capability("testing")
    assert len(results) == 2
    agent_ids = {agent.agent_id for agent in results}
    assert agent_ids == {"agent1", "agent2"}


def test_registry_find_by_capability_no_matches(registry: AgentRegistry, sample_spec: AgentSpec):
    """Test find_by_capability() returns empty list for no matches."""
    registry.register(sample_spec)

    results = registry.find_by_capability("nonexistent_capability")
    assert results == []


def test_registry_clear(registry: AgentRegistry, sample_spec: AgentSpec):
    """Test clear() removes all agents and default."""
    registry.register_default(sample_spec)

    assert len(registry.list_agents()) == 1

    registry.clear()

    assert len(registry.list_agents()) == 0
    with pytest.raises(RuntimeError, match="No default agent registered"):
        registry.get_default()


def test_get_registry_returns_singleton():
    """Test that get_registry() returns the same instance."""
    reg1 = get_registry()
    reg2 = get_registry()

    assert reg1 is reg2
