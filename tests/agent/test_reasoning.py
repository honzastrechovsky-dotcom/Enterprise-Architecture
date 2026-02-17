"""Tests for ReasoningEngine OBSERVE→THINK→VERIFY loop.

Tests cover:
- OBSERVE phase extracts correct context
- THINK phase generates reasoning steps
- VERIFY phase checks assertions
- Full OBSERVE→THINK→VERIFY loop
- Mock LLM calls with canned responses
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from src.agent.llm import LLMClient
from src.agent.reasoning import (
    Observation,
    ReasoningEngine,
    SpecialistReasoningResult,
    ReasoningStep,
    Verification,
)
from src.agent.registry import AgentSpec
from src.config import Settings
from src.models.user import UserRole


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def mock_settings():
    """Mock settings for reasoning engine."""
    return Settings(
        environment="test",
        database_url="postgresql://test",
        litellm_base_url="http://localhost:4000",
        litellm_api_key="sk-test",
    )


@pytest.fixture
def mock_llm_client():
    """Mock LLM client for testing."""
    client = Mock(spec=LLMClient)
    client.complete = AsyncMock()
    client.extract_text = Mock()
    return client


@pytest.fixture
def reasoning_engine(mock_llm_client, mock_settings):
    """Create ReasoningEngine with mocked dependencies."""
    return ReasoningEngine(llm_client=mock_llm_client, settings=mock_settings)


@pytest.fixture
def sample_agent_spec():
    """Sample AgentSpec for testing."""
    return AgentSpec(
        agent_id="test_agent",
        name="Test Agent",
        description="Test agent for reasoning",
        system_prompt="You are a test agent.",
        capabilities=["testing"],
        tools=[],
        requires_verification=True,
    )


# ------------------------------------------------------------------ #
# OBSERVE phase tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_observe_extracts_facts(reasoning_engine, mock_llm_client):
    """Test that OBSERVE phase extracts key facts from query and context."""
    # Mock LLM response for observation
    observe_response = {
        "key_facts": ["Fact 1", "Fact 2", "Fact 3"],
        "assumptions": ["Assumption 1"],
        "uncertainties": ["Uncertainty 1"],
        "data_sources": ["Source A", "Source B"],
    }

    mock_llm_response = Mock()
    mock_llm_client.extract_text.return_value = json.dumps(observe_response)
    mock_llm_client.complete.return_value = mock_llm_response

    observation = await reasoning_engine._observe(
        query="How do I calibrate sensor X?",
        context="Sensor X documentation says...",
    )

    assert len(observation.key_facts) == 3
    assert "Fact 1" in observation.key_facts
    assert len(observation.assumptions) == 1
    assert len(observation.uncertainties) == 1
    assert len(observation.data_sources) == 2


@pytest.mark.asyncio
async def test_observe_handles_json_parse_error(reasoning_engine, mock_llm_client):
    """Test that OBSERVE phase handles JSON parse errors gracefully."""
    mock_llm_response = Mock()
    mock_llm_client.extract_text.return_value = "Not valid JSON!"
    mock_llm_client.complete.return_value = mock_llm_response

    observation = await reasoning_engine._observe(
        query="Test query",
        context="Test context",
    )

    # Should return fallback observation
    assert len(observation.key_facts) == 1
    assert "Could not extract facts" in observation.key_facts[0]
    assert len(observation.uncertainties) == 1


@pytest.mark.asyncio
async def test_observe_handles_llm_error(reasoning_engine, mock_llm_client):
    """Test that OBSERVE phase handles LLM errors gracefully."""
    mock_llm_client.complete.side_effect = Exception("LLM error")

    observation = await reasoning_engine._observe(
        query="Test query",
        context="Test context",
    )

    # Should return error observation
    assert len(observation.key_facts) == 1
    assert "Error during observation" in observation.key_facts[0]


# ------------------------------------------------------------------ #
# THINK phase tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_think_generates_reasoning_steps(reasoning_engine, mock_llm_client):
    """Test that THINK phase generates reasoning steps."""
    # Mock LLM response for thinking
    think_response = {
        "steps": [
            {
                "step_number": 1,
                "description": "First reasoning step",
                "evidence": ["Evidence A", "Evidence B"],
                "conclusion": "Intermediate conclusion",
                "confidence": 0.85,
            },
            {
                "step_number": 2,
                "description": "Second reasoning step",
                "evidence": ["Evidence C"],
                "conclusion": "Final conclusion",
                "confidence": 0.90,
            },
        ]
    }

    mock_llm_response = Mock()
    mock_llm_client.extract_text.return_value = json.dumps(think_response)
    mock_llm_client.complete.return_value = mock_llm_response

    observation = Observation(
        key_facts=["Fact 1", "Fact 2"],
        assumptions=["Assumption 1"],
        uncertainties=["Uncertainty 1"],
        data_sources=["Source A"],
    )

    steps = await reasoning_engine._think(observation, query="Test query")

    assert len(steps) == 2
    assert steps[0].step_number == 1
    assert steps[0].confidence == 0.85
    assert "First reasoning step" in steps[0].description
    assert len(steps[0].evidence) == 2


@pytest.mark.asyncio
async def test_think_handles_json_parse_error(reasoning_engine, mock_llm_client):
    """Test that THINK phase handles JSON parse errors gracefully."""
    mock_llm_response = Mock()
    mock_llm_client.extract_text.return_value = "Not valid JSON!"
    mock_llm_client.complete.return_value = mock_llm_response

    observation = Observation(
        key_facts=["Fact 1"],
        assumptions=[],
        uncertainties=[],
        data_sources=[],
    )

    steps = await reasoning_engine._think(observation, query="Test query")

    # Should return fallback step
    assert len(steps) == 1
    assert steps[0].confidence == 0.3
    assert "Unable to build structured reasoning" in steps[0].description


# ------------------------------------------------------------------ #
# VERIFY phase tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_verify_checks_reasoning_chain(reasoning_engine, mock_llm_client, sample_agent_spec):
    """Test that VERIFY phase checks reasoning chain."""
    # Mock LLM response for verification
    verify_response = {
        "is_verified": True,
        "checks_passed": ["Internal consistency", "Evidence backing"],
        "checks_failed": [],
        "requires_human_review": False,
        "review_reason": None,
    }

    mock_llm_response = Mock()
    mock_llm_client.extract_text.return_value = json.dumps(verify_response)
    mock_llm_client.complete.return_value = mock_llm_response

    steps = [
        ReasoningStep(
            step_number=1,
            description="Step 1",
            evidence=["Evidence 1"],
            conclusion="Conclusion 1",
            confidence=0.8,
        )
    ]

    observation = Observation(
        key_facts=["Fact 1"],
        assumptions=[],
        uncertainties=[],
        data_sources=[],
    )

    verification = await reasoning_engine._verify(
        steps=steps,
        observation=observation,
        agent_spec=sample_agent_spec,
    )

    assert verification.is_verified is True
    assert len(verification.checks_passed) == 2
    assert len(verification.checks_failed) == 0
    assert verification.requires_human_review is False


@pytest.mark.asyncio
async def test_verify_flags_failures(reasoning_engine, mock_llm_client, sample_agent_spec):
    """Test that VERIFY phase flags failures."""
    # Mock LLM response with failures
    verify_response = {
        "is_verified": False,
        "checks_passed": ["Internal consistency"],
        "checks_failed": ["Evidence backing weak"],
        "requires_human_review": True,
        "review_reason": "Weak evidence for critical claim",
    }

    mock_llm_response = Mock()
    mock_llm_client.extract_text.return_value = json.dumps(verify_response)
    mock_llm_client.complete.return_value = mock_llm_response

    steps = [
        ReasoningStep(
            step_number=1,
            description="Step 1",
            evidence=["Weak evidence"],
            conclusion="Conclusion 1",
            confidence=0.4,
        )
    ]

    observation = Observation(
        key_facts=["Fact 1"],
        assumptions=[],
        uncertainties=[],
        data_sources=[],
    )

    verification = await reasoning_engine._verify(
        steps=steps,
        observation=observation,
        agent_spec=sample_agent_spec,
    )

    assert verification.is_verified is False
    assert len(verification.checks_failed) == 1
    assert verification.requires_human_review is True
    assert verification.review_reason is not None


@pytest.mark.asyncio
async def test_verify_handles_json_parse_error(reasoning_engine, mock_llm_client, sample_agent_spec):
    """Test that VERIFY phase handles JSON parse errors conservatively."""
    mock_llm_response = Mock()
    mock_llm_client.extract_text.return_value = "Not valid JSON!"
    mock_llm_client.complete.return_value = mock_llm_response

    steps = [
        ReasoningStep(
            step_number=1,
            description="Step 1",
            evidence=["Evidence 1"],
            conclusion="Conclusion 1",
            confidence=0.8,
        )
    ]

    observation = Observation(
        key_facts=["Fact 1"],
        assumptions=[],
        uncertainties=[],
        data_sources=[],
    )

    verification = await reasoning_engine._verify(
        steps=steps,
        observation=observation,
        agent_spec=sample_agent_spec,
    )

    # Conservative fallback: fail verification, require review
    assert verification.is_verified is False
    assert verification.requires_human_review is True
    assert "JSON parse error" in verification.review_reason


# ------------------------------------------------------------------ #
# Full reasoning loop tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_full_reasoning_loop_success(reasoning_engine, mock_llm_client, sample_agent_spec):
    """Test full OBSERVE→THINK→VERIFY loop succeeds."""
    # Setup mocked responses for all three phases
    responses = [
        # OBSERVE response
        json.dumps({
            "key_facts": ["Fact 1", "Fact 2"],
            "assumptions": ["Assumption 1"],
            "uncertainties": ["Uncertainty 1"],
            "data_sources": ["Source A"],
        }),
        # THINK response
        json.dumps({
            "steps": [
                {
                    "step_number": 1,
                    "description": "Reasoning step",
                    "evidence": ["Evidence 1"],
                    "conclusion": "Final conclusion",
                    "confidence": 0.85,
                }
            ]
        }),
        # VERIFY response
        json.dumps({
            "is_verified": True,
            "checks_passed": ["All checks passed"],
            "checks_failed": [],
            "requires_human_review": False,
            "review_reason": None,
        }),
    ]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await reasoning_engine.reason(
        query="How do I perform task X?",
        context="Context about task X...",
        agent_spec=sample_agent_spec,
        require_verification=True,
    )

    assert isinstance(result, SpecialistReasoningResult)
    assert len(result.observation.key_facts) == 2
    assert len(result.reasoning_steps) == 1
    assert result.conclusion == "Final conclusion"
    assert result.verification.is_verified is True
    assert result.total_confidence == 0.85


@pytest.mark.asyncio
async def test_reasoning_enforces_verification_for_safety_critical(
    reasoning_engine, mock_llm_client, sample_agent_spec
):
    """Test that reasoning enforces verification for safety-critical agents."""
    # Setup responses with low confidence
    responses = [
        json.dumps({
            "key_facts": ["Fact 1"],
            "assumptions": [],
            "uncertainties": [],
            "data_sources": [],
        }),
        json.dumps({
            "steps": [
                {
                    "step_number": 1,
                    "description": "Step",
                    "evidence": ["Evidence"],
                    "conclusion": "Conclusion",
                    "confidence": 0.5,  # Low confidence
                }
            ]
        }),
        json.dumps({
            "is_verified": True,
            "checks_passed": ["Check"],
            "checks_failed": [],
            "requires_human_review": False,
            "review_reason": None,
        }),
    ]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = responses
    mock_llm_client.complete.return_value = mock_llm_response

    # Mark agent as safety-critical
    sample_agent_spec.requires_verification = True

    result = await reasoning_engine.reason(
        query="Safety-critical task",
        context="Context",
        agent_spec=sample_agent_spec,
        require_verification=True,
    )

    # Should flag for human review due to low confidence on safety-critical agent
    assert result.verification.requires_human_review is True
    assert result.total_confidence < 0.6


@pytest.mark.asyncio
async def test_reasoning_calculates_total_confidence(reasoning_engine, mock_llm_client, sample_agent_spec):
    """Test that reasoning calculates total confidence correctly."""
    responses = [
        json.dumps({
            "key_facts": ["Fact 1"],
            "assumptions": [],
            "uncertainties": [],
            "data_sources": [],
        }),
        json.dumps({
            "steps": [
                {
                    "step_number": 1,
                    "description": "Step 1",
                    "evidence": [],
                    "conclusion": "Conclusion 1",
                    "confidence": 0.8,
                },
                {
                    "step_number": 2,
                    "description": "Step 2",
                    "evidence": [],
                    "conclusion": "Conclusion 2",
                    "confidence": 0.9,
                },
            ]
        }),
        json.dumps({
            "is_verified": True,
            "checks_passed": [],
            "checks_failed": [],
            "requires_human_review": False,
            "review_reason": None,
        }),
    ]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await reasoning_engine.reason(
        query="Query",
        context="Context",
        agent_spec=sample_agent_spec,
        require_verification=False,
    )

    # Average of 0.8 and 0.9
    assert abs(result.total_confidence - 0.85) < 1e-9


@pytest.mark.asyncio
async def test_reasoning_handles_no_reasoning_steps(reasoning_engine, mock_llm_client, sample_agent_spec):
    """Test that reasoning handles case with no reasoning steps."""
    responses = [
        json.dumps({
            "key_facts": ["Fact 1"],
            "assumptions": [],
            "uncertainties": [],
            "data_sources": [],
        }),
        json.dumps({"steps": []}),  # No steps generated
        json.dumps({
            "is_verified": False,
            "checks_passed": [],
            "checks_failed": ["No reasoning steps"],
            "requires_human_review": True,
            "review_reason": "No reasoning steps generated",
        }),
    ]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await reasoning_engine.reason(
        query="Query",
        context="Context",
        agent_spec=sample_agent_spec,
        require_verification=True,
    )

    assert len(result.reasoning_steps) == 0
    assert result.total_confidence == 0.0
    assert result.conclusion == "No conclusion reached"
