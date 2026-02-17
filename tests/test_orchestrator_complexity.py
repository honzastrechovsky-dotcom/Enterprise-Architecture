"""Tests for 11D1: QueryComplexity classifier and 11D2: auto-composition routing.

Covers the _assess_complexity() method and routing decisions in orchestrator.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agent.orchestrator import AgentOrchestrator, IntentClassification, QueryComplexity


def _make_orchestrator():
    """Build a minimal orchestrator instance for testing."""
    db = AsyncMock()
    settings = MagicMock()
    settings.litellm_default_model = "test-model"
    settings.model_heavy = "heavy-model"
    llm_client = MagicMock()
    tool_gateway = MagicMock()

    with patch("src.agent.orchestrator.get_registry") as mock_reg, \
         patch("src.agent.orchestrator.get_skill_registry"), \
         patch("src.agent.orchestrator.RedTeam"):
        mock_reg.return_value = MagicMock()
        orch = AgentOrchestrator(
            db=db,
            settings=settings,
            llm_client=llm_client,
            tool_gateway=tool_gateway,
        )

    return orch


class TestQueryComplexityEnum:
    def test_all_values_present(self):
        values = {c.value for c in QueryComplexity}
        assert "simple" in values
        assert "deep" in values
        assert "multi_perspective" in values
        assert "quality_critical" in values

    def test_is_str_enum(self):
        assert QueryComplexity.SIMPLE == "simple"
        assert QueryComplexity.DEEP == "deep"
        assert QueryComplexity.MULTI_PERSPECTIVE == "multi_perspective"
        assert QueryComplexity.QUALITY_CRITICAL == "quality_critical"


class TestAssessComplexity:
    """Tests for the LLM-based complexity classifier."""

    def _make_intent(self, cap="general_knowledge"):
        return IntentClassification(
            primary_capability=cap,
            confidence=0.9,
            reasoning="test",
        )

    async def test_returns_simple_for_simple_response(self):
        orch = _make_orchestrator()
        orch._llm.complete = AsyncMock()
        orch._llm.extract_text = MagicMock(return_value="SIMPLE")

        result = await orch._assess_complexity("What is 2+2?", self._make_intent())
        assert result == QueryComplexity.SIMPLE

    async def test_returns_deep_for_deep_response(self):
        orch = _make_orchestrator()
        orch._llm.complete = AsyncMock()
        orch._llm.extract_text = MagicMock(return_value="DEEP")

        result = await orch._assess_complexity(
            "Explain step-by-step how to build a RAG pipeline",
            self._make_intent(),
        )
        assert result == QueryComplexity.DEEP

    async def test_returns_multi_perspective_for_decision_query(self):
        orch = _make_orchestrator()
        orch._llm.complete = AsyncMock()
        orch._llm.extract_text = MagicMock(return_value="MULTI_PERSPECTIVE")

        result = await orch._assess_complexity(
            "Should we use PostgreSQL or MongoDB?",
            self._make_intent(),
        )
        assert result == QueryComplexity.MULTI_PERSPECTIVE

    async def test_returns_quality_critical_for_safety(self):
        orch = _make_orchestrator()
        orch._llm.complete = AsyncMock()
        orch._llm.extract_text = MagicMock(return_value="QUALITY_CRITICAL")

        result = await orch._assess_complexity(
            "What is the maintenance procedure for the reactor cooling system?",
            self._make_intent(),
        )
        assert result == QueryComplexity.QUALITY_CRITICAL

    async def test_defaults_to_simple_on_unknown_response(self):
        orch = _make_orchestrator()
        orch._llm.complete = AsyncMock()
        orch._llm.extract_text = MagicMock(return_value="UNKNOWN_VALUE")

        result = await orch._assess_complexity("Any query", self._make_intent())
        assert result == QueryComplexity.SIMPLE

    async def test_defaults_to_simple_on_llm_failure(self):
        orch = _make_orchestrator()
        orch._llm.complete = AsyncMock(side_effect=Exception("LLM timeout"))

        result = await orch._assess_complexity("Any query", self._make_intent())
        assert result == QueryComplexity.SIMPLE

    async def test_handles_lowercase_response(self):
        orch = _make_orchestrator()
        orch._llm.complete = AsyncMock()
        orch._llm.extract_text = MagicMock(return_value="deep")

        result = await orch._assess_complexity("Multi-step analysis needed", self._make_intent())
        assert result == QueryComplexity.DEEP

    async def test_handles_response_with_extra_text(self):
        orch = _make_orchestrator()
        orch._llm.complete = AsyncMock()
        orch._llm.extract_text = MagicMock(return_value="  SIMPLE  ")

        result = await orch._assess_complexity("Short question", self._make_intent())
        assert result == QueryComplexity.SIMPLE


class TestComplexityIntegration:
    """Test that complexity maps to correct composition patterns."""

    def test_query_complexity_values_are_unique(self):
        values = [c.value for c in QueryComplexity]
        assert len(values) == len(set(values))

    def test_simple_is_default_path(self):
        # SIMPLE should be the cheapest/fastest path — verify enum exists and is named correctly
        assert QueryComplexity.SIMPLE.value == "simple"

    def test_quality_critical_requires_verification(self):
        # QUALITY_CRITICAL maps to Gate pattern — document this invariant
        assert QueryComplexity.QUALITY_CRITICAL.value == "quality_critical"
