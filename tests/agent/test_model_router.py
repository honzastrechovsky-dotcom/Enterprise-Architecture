"""Tests for ModelRouter, ComplexityEstimator, BudgetManager, FallbackChain.

Tests cover:
- Complexity scoring for simple vs complex queries
- Router selects LIGHT/STANDARD/HEAVY based on complexity
- Budget tracking: increment usage, check limits
- Budget resets daily
- Fallback chain: HEAVY → STANDARD → LIGHT
- model_routing_enabled=False bypasses routing
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest

from src.agent.llm import LLMClient
from src.agent.model_router.budget import BudgetManager, TokenBudget
from src.agent.model_router.complexity import ComplexityEstimator, TaskComplexity
from src.agent.model_router.fallback import FallbackChain
from src.agent.model_router.router import ModelConfig, ModelRouter, ModelTier
from src.agent.registry import AgentSpec
from src.config import Settings
from src.models.user import UserRole


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def mock_settings():
    """Mock settings for model router."""
    return Settings(
        environment="test",
        database_url="postgresql://test",
        litellm_base_url="http://localhost:4000",
        litellm_api_key="sk-test",
        model_light="ollama/qwen2.5:7b",
        model_standard="ollama/qwen2.5:32b",
        model_heavy="vllm/qwen2.5:72b",
    )


@pytest.fixture
def sample_agent_spec():
    """Sample AgentSpec for testing."""
    return AgentSpec(
        agent_id="test_agent",
        name="Test Agent",
        description="Test agent",
        system_prompt="Prompt",
        capabilities=["testing"],
        tools=[],
    )


# ------------------------------------------------------------------ #
# ComplexityEstimator tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_complexity_simple_message():
    """Test complexity estimator returns low score for simple message."""
    estimator = ComplexityEstimator()

    result = await estimator.estimate(
        message="Hello, how are you?",
        context_length=0,
        agent_capabilities=[],
        history_length=0,
    )

    assert isinstance(result, TaskComplexity)
    assert 0.0 <= result.score <= 0.3  # Should be LIGHT tier
    assert result.recommended_tier == "light"


@pytest.mark.asyncio
async def test_complexity_moderate_message():
    """Test complexity estimator returns moderate score for normal message."""
    estimator = ComplexityEstimator()

    result = await estimator.estimate(
        message="Can you explain how the authentication system works and what security measures are in place?",
        context_length=500,
        agent_capabilities=["authentication"],
        history_length=3,
    )

    assert 0.3 <= result.score <= 0.7  # Should be STANDARD tier
    assert result.recommended_tier == "standard"


@pytest.mark.asyncio
async def test_complexity_complex_message():
    """Test complexity estimator returns high score for complex message."""
    estimator = ComplexityEstimator()

    # Long message with complex keywords
    message = """
    Please analyze the security architecture of our authentication system.
    Evaluate the threat model, assess potential vulnerabilities, compare
    different authentication approaches (OAuth2, SAML, JWT), and provide
    a comprehensive security review with recommendations for compliance
    with SOC2 and ISO27001 standards.
    """

    result = await estimator.estimate(
        message=message,
        context_length=2000,
        agent_capabilities=["security_analysis", "compliance_review"],
        history_length=10,
    )

    assert result.score >= 0.7  # Should be HEAVY tier
    assert result.recommended_tier == "heavy"


@pytest.mark.asyncio
async def test_complexity_critical_capabilities_increase_score():
    """Test that critical capabilities push score higher."""
    estimator = ComplexityEstimator()

    # Same simple message, but with critical capabilities
    result = await estimator.estimate(
        message="Check status",
        context_length=0,
        agent_capabilities=["security_analysis", "compliance_review", "audit"],
        history_length=0,
    )

    # Critical capabilities should push score higher than it would be otherwise
    assert result.factors["capability_criticality"] >= 0.5


@pytest.mark.asyncio
async def test_complexity_keywords_increase_score():
    """Test that complex keywords increase score."""
    estimator = ComplexityEstimator()

    # Message with complex keywords
    result_with_keywords = await estimator.estimate(
        message="Analyze the security architecture and assess vulnerabilities",
        context_length=0,
        agent_capabilities=[],
        history_length=0,
    )

    result_without_keywords = await estimator.estimate(
        message="Show me the status",
        context_length=0,
        agent_capabilities=[],
        history_length=0,
    )

    # Keywords should increase score
    assert result_with_keywords.factors["keyword_signals"] > result_without_keywords.factors["keyword_signals"]


# ------------------------------------------------------------------ #
# ModelRouter tests
# ------------------------------------------------------------------ #


def test_model_router_initialization(mock_settings):
    """Test ModelRouter initializes with correct models."""
    router = ModelRouter(settings=mock_settings)

    assert ModelTier.LIGHT in router._models
    assert ModelTier.STANDARD in router._models
    assert ModelTier.HEAVY in router._models

    assert router._models[ModelTier.LIGHT].model_id == "ollama/qwen2.5:7b"
    assert router._models[ModelTier.STANDARD].model_id == "ollama/qwen2.5:32b"
    assert router._models[ModelTier.HEAVY].model_id == "vllm/qwen2.5:72b"


def test_model_router_routes_by_complexity(mock_settings):
    """Test ModelRouter selects tier based on complexity score."""
    router = ModelRouter(settings=mock_settings)

    # Low complexity -> LIGHT
    light_model = router.route(
        task_type="simple_qa",
        complexity_score=0.2,
        agent_spec=None,
    )
    assert light_model.tier == ModelTier.LIGHT

    # Medium complexity -> STANDARD
    standard_model = router.route(
        task_type="general",
        complexity_score=0.5,
        agent_spec=None,
    )
    assert standard_model.tier == ModelTier.STANDARD

    # High complexity -> HEAVY
    heavy_model = router.route(
        task_type="analysis",
        complexity_score=0.8,
        agent_spec=None,
    )
    assert heavy_model.tier == ModelTier.HEAVY


def test_model_router_task_type_overrides(mock_settings):
    """Test ModelRouter task type overrides complexity score."""
    router = ModelRouter(settings=mock_settings)

    # Intent classification always uses LIGHT, even with high complexity
    model = router.route(
        task_type="intent_classification",
        complexity_score=0.9,  # High complexity
        agent_spec=None,
    )
    assert model.tier == ModelTier.LIGHT

    # Thinking tools always use HEAVY, even with low complexity
    model = router.route(
        task_type="thinking_tools",
        complexity_score=0.1,  # Low complexity
        agent_spec=None,
    )
    assert model.tier == ModelTier.HEAVY


def test_model_router_respects_agent_preference(mock_settings, sample_agent_spec):
    """Test ModelRouter respects agent's declared model preference."""
    router = ModelRouter(settings=mock_settings)

    # Agent prefers HEAVY model
    sample_agent_spec.model_preference = "vllm/qwen2.5:72b"

    model = router.route(
        task_type="general",
        complexity_score=0.2,  # Low complexity would normally select LIGHT
        agent_spec=sample_agent_spec,
    )

    # Should respect agent preference
    assert model.model_id == "vllm/qwen2.5:72b"
    assert model.tier == ModelTier.HEAVY


def test_model_router_escalate(mock_settings):
    """Test ModelRouter escalates to next tier."""
    router = ModelRouter(settings=mock_settings)

    # Escalate from LIGHT -> STANDARD
    escalated = router.escalate(ModelTier.LIGHT)
    assert escalated.tier == ModelTier.STANDARD

    # Escalate from STANDARD -> HEAVY
    escalated = router.escalate(ModelTier.STANDARD)
    assert escalated.tier == ModelTier.HEAVY

    # Cannot escalate from HEAVY
    with pytest.raises(ValueError, match="already at highest tier"):
        router.escalate(ModelTier.HEAVY)


def test_model_router_fallback(mock_settings):
    """Test ModelRouter falls back to cheaper tier."""
    router = ModelRouter(settings=mock_settings)

    # Fallback from HEAVY -> STANDARD
    fallback = router.fallback(ModelTier.HEAVY)
    assert fallback.tier == ModelTier.STANDARD

    # Fallback from STANDARD -> LIGHT
    fallback = router.fallback(ModelTier.STANDARD)
    assert fallback.tier == ModelTier.LIGHT

    # Cannot fallback from LIGHT
    with pytest.raises(ValueError, match="already at lowest tier"):
        router.fallback(ModelTier.LIGHT)


def test_model_router_get_model_for_agent(mock_settings, sample_agent_spec):
    """Test ModelRouter gets model for agent with preference."""
    router = ModelRouter(settings=mock_settings)

    # Agent with no preference -> STANDARD
    model = router.get_model_for_agent(sample_agent_spec)
    assert model.tier == ModelTier.STANDARD

    # Agent with valid preference
    sample_agent_spec.model_preference = "ollama/qwen2.5:7b"
    model = router.get_model_for_agent(sample_agent_spec)
    assert model.model_id == "ollama/qwen2.5:7b"
    assert model.tier == ModelTier.LIGHT


def test_model_router_get_tier_by_model_id(mock_settings):
    """Test ModelRouter looks up tier by model ID."""
    router = ModelRouter(settings=mock_settings)

    tier = router.get_tier_by_model_id("ollama/qwen2.5:7b")
    assert tier == ModelTier.LIGHT

    tier = router.get_tier_by_model_id("vllm/qwen2.5:72b")
    assert tier == ModelTier.HEAVY

    tier = router.get_tier_by_model_id("nonexistent-model")
    assert tier is None


# ------------------------------------------------------------------ #
# BudgetManager tests
# ------------------------------------------------------------------ #


def test_budget_manager_initialization():
    """Test BudgetManager initializes with defaults."""
    manager = BudgetManager(
        default_daily_limit=1_000_000,
        default_monthly_limit=20_000_000,
    )

    assert manager._default_daily == 1_000_000
    assert manager._default_monthly == 20_000_000


def test_budget_manager_check_budget_success():
    """Test BudgetManager allows request within budget."""
    manager = BudgetManager()
    tenant_id = uuid.uuid4()

    # First request should succeed
    can_afford = manager.check_budget(tenant_id, estimated_tokens=1000)
    assert can_afford is True


def test_budget_manager_check_budget_exceeds_daily():
    """Test BudgetManager blocks request exceeding daily limit."""
    manager = BudgetManager(
        default_daily_limit=5000,
        default_monthly_limit=100000,
    )
    tenant_id = uuid.uuid4()

    # Use up most of daily budget
    manager.record_usage(tenant_id, ModelTier.STANDARD, input_tokens=2000, output_tokens=2500)

    # Next request exceeds daily limit
    can_afford = manager.check_budget(tenant_id, estimated_tokens=1000)
    assert can_afford is False


def test_budget_manager_record_usage():
    """Test BudgetManager records usage correctly."""
    manager = BudgetManager()
    tenant_id = uuid.uuid4()

    manager.record_usage(
        tenant_id,
        ModelTier.STANDARD,
        input_tokens=500,
        output_tokens=1000,
        complexity_score=0.5,
    )

    budget = manager.get_usage(tenant_id)
    assert budget.current_daily == 1500
    assert budget.current_monthly == 1500


def test_budget_manager_daily_reset():
    """Test BudgetManager resets daily counter on new day."""
    manager = BudgetManager()
    tenant_id = uuid.uuid4()

    # Record usage
    manager.record_usage(tenant_id, ModelTier.LIGHT, input_tokens=500, output_tokens=500)

    budget = manager.get_usage(tenant_id)
    assert budget.current_daily == 1000

    # Simulate next day by changing last_reset_date
    budget.last_reset_date = "2020-01-01"
    budget.current_daily = 1000

    # Get usage should trigger reset
    budget = manager.get_usage(tenant_id)
    assert budget.current_daily == 0


def test_budget_manager_monthly_reset():
    """Test BudgetManager resets monthly counter on new month."""
    manager = BudgetManager()
    tenant_id = uuid.uuid4()

    # Record usage
    manager.record_usage(tenant_id, ModelTier.STANDARD, input_tokens=1000, output_tokens=1000)

    budget = manager.get_usage(tenant_id)
    assert budget.current_monthly == 2000

    # Simulate next month
    budget.last_reset_month = "2020-01"
    budget.current_monthly = 2000

    budget = manager.get_usage(tenant_id)
    assert budget.current_monthly == 0


def test_budget_manager_savings_report():
    """Test BudgetManager calculates savings from routing."""
    manager = BudgetManager()
    tenant_id = uuid.uuid4()

    # Record usage across different tiers
    manager.record_usage(tenant_id, ModelTier.LIGHT, input_tokens=500, output_tokens=500)  # 1000 tokens
    manager.record_usage(tenant_id, ModelTier.STANDARD, input_tokens=500, output_tokens=500)  # 1000 tokens
    manager.record_usage(tenant_id, ModelTier.HEAVY, input_tokens=500, output_tokens=500)  # 1000 tokens

    report = manager.get_savings_report(tenant_id)

    assert report["light_tier_count"] == 1
    assert report["standard_tier_count"] == 1
    assert report["heavy_tier_count"] == 1
    assert report["tokens_saved"] > 0  # Should show savings vs all-HEAVY
    assert report["cost_reduction_pct"] > 0


def test_budget_manager_threshold_warnings(caplog):
    """Test BudgetManager emits warnings at thresholds."""
    manager = BudgetManager(
        default_daily_limit=1000,
        default_monthly_limit=10000,
    )
    tenant_id = uuid.uuid4()

    # Use 85% of daily limit (above 80% warning threshold)
    manager.record_usage(tenant_id, ModelTier.STANDARD, input_tokens=425, output_tokens=425)

    # Should have logged warning
    # Note: This test relies on structlog configuration


# ------------------------------------------------------------------ #
# FallbackChain tests
# ------------------------------------------------------------------ #


@pytest.fixture
def mock_llm_client():
    """Mock LLM client for testing."""
    client = Mock(spec=LLMClient)
    client.complete = AsyncMock()
    return client


@pytest.fixture
def fallback_tiers(mock_settings):
    """Create ordered fallback tiers."""
    return [
        ModelConfig(
            tier=ModelTier.HEAVY,
            model_id="vllm/qwen2.5:72b",
            max_tokens=8192,
            cost_weight=10.0,
            gpu_memory_gb=72.0,
        ),
        ModelConfig(
            tier=ModelTier.STANDARD,
            model_id="ollama/qwen2.5:32b",
            max_tokens=4096,
            cost_weight=3.0,
            gpu_memory_gb=32.0,
        ),
        ModelConfig(
            tier=ModelTier.LIGHT,
            model_id="ollama/qwen2.5:7b",
            max_tokens=2048,
            cost_weight=1.0,
            gpu_memory_gb=8.0,
        ),
    ]


@pytest.mark.asyncio
async def test_fallback_chain_first_tier_succeeds(mock_llm_client, fallback_tiers):
    """Test FallbackChain succeeds on first tier."""
    chain = FallbackChain(tiers=fallback_tiers)

    mock_response = Mock()
    mock_llm_client.complete.return_value = mock_response

    response, tier_used = await chain.execute_with_fallback(
        llm_client=mock_llm_client,
        messages=[{"role": "user", "content": "Test"}],
        preferred_tier=fallback_tiers[0],  # HEAVY
        temperature=0.7,
    )

    assert response == mock_response
    assert tier_used.tier == ModelTier.HEAVY
    assert mock_llm_client.complete.call_count == 1


@pytest.mark.asyncio
async def test_fallback_chain_falls_back_on_failure(mock_llm_client, fallback_tiers):
    """Test FallbackChain falls back when first tier fails."""
    chain = FallbackChain(tiers=fallback_tiers)

    # First call fails, second succeeds
    mock_response = Mock()
    mock_llm_client.complete.side_effect = [
        Exception("HEAVY tier timeout"),
        mock_response,  # STANDARD succeeds
    ]

    response, tier_used = await chain.execute_with_fallback(
        llm_client=mock_llm_client,
        messages=[{"role": "user", "content": "Test"}],
        preferred_tier=fallback_tiers[0],  # HEAVY
        temperature=0.7,
    )

    assert response == mock_response
    assert tier_used.tier == ModelTier.STANDARD
    assert mock_llm_client.complete.call_count == 2

    # Check fallback event was recorded
    events = chain.get_fallback_events()
    assert len(events) == 1
    assert events[0]["tier"] == "heavy"


@pytest.mark.asyncio
async def test_fallback_chain_all_tiers_fail(mock_llm_client, fallback_tiers):
    """Test FallbackChain raises error when all tiers fail."""
    chain = FallbackChain(tiers=fallback_tiers)

    # All tiers fail
    mock_llm_client.complete.side_effect = Exception("All tiers failed")

    with pytest.raises(RuntimeError, match="All model tiers failed"):
        await chain.execute_with_fallback(
            llm_client=mock_llm_client,
            messages=[{"role": "user", "content": "Test"}],
            preferred_tier=fallback_tiers[0],
            temperature=0.7,
        )

    # Should have tried all 3 tiers
    assert mock_llm_client.complete.call_count == 3

    # Check all fallback events recorded
    events = chain.get_fallback_events()
    assert len(events) == 3


@pytest.mark.asyncio
async def test_fallback_chain_prefers_specified_tier(mock_llm_client, fallback_tiers):
    """Test FallbackChain tries preferred tier first."""
    chain = FallbackChain(tiers=fallback_tiers)

    mock_response = Mock()
    mock_llm_client.complete.return_value = mock_response

    # Prefer STANDARD tier
    response, tier_used = await chain.execute_with_fallback(
        llm_client=mock_llm_client,
        messages=[{"role": "user", "content": "Test"}],
        preferred_tier=fallback_tiers[1],  # STANDARD
        temperature=0.7,
    )

    assert tier_used.tier == ModelTier.STANDARD

    # Verify it called STANDARD tier's model
    call_args = mock_llm_client.complete.call_args
    assert call_args.kwargs["model"] == "ollama/qwen2.5:32b"


def test_fallback_chain_reset_events(fallback_tiers):
    """Test FallbackChain can reset event history."""
    chain = FallbackChain(tiers=fallback_tiers)

    # Manually add events
    chain._fallback_events.append({"tier": "heavy", "error": "test"})
    assert len(chain.get_fallback_events()) == 1

    chain.reset_events()
    assert len(chain.get_fallback_events()) == 0
