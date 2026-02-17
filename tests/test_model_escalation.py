"""Tests for 11D3: Model escalation on uncertainty.

Covers the _call_with_escalation() function in runtime.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agent.runtime import _ESCALATION_MIN_LENGTH, _UNCERTAINTY_MARKERS, _call_with_escalation


def _make_llm(response_text: str, model_name: str = "test-model"):
    """Create a mock LLM client that returns a fixed response."""
    llm = MagicMock()
    mock_response = MagicMock()
    llm.complete = AsyncMock(return_value=mock_response)
    llm.extract_text = MagicMock(return_value=response_text)
    llm.extract_model_name = MagicMock(return_value=model_name)
    return llm


class TestEscalationConstants:
    def test_uncertainty_markers_are_lowercase(self):
        for marker in _UNCERTAINTY_MARKERS:
            assert marker == marker.lower(), f"Marker {marker!r} is not lowercase"

    def test_escalation_min_length_is_positive(self):
        assert _ESCALATION_MIN_LENGTH > 0

    def test_escalation_threshold_is_reasonable(self):
        # Should be a short response — not too large
        assert _ESCALATION_MIN_LENGTH <= 500


class TestCallWithEscalation:
    async def test_returns_light_model_for_confident_response(self):
        confident_text = "The answer is 42. " * 20  # Long confident response
        llm = _make_llm(confident_text, "light-model")

        response, model = await _call_with_escalation(
            llm_client=llm,
            messages=[{"role": "user", "content": "What is 6*7?"}],
            model_light="light-model",
            model_heavy="heavy-model",
        )
        assert model == "light-model"
        # Only one LLM call (no escalation)
        assert llm.complete.call_count == 1

    async def test_escalates_on_short_uncertain_response(self):
        uncertain_text = "I'm not sure about this."  # Short + uncertain
        assert len(uncertain_text) < _ESCALATION_MIN_LENGTH

        # First call returns uncertain, second call returns heavy model
        llm = MagicMock()
        mock_light = MagicMock()
        mock_heavy = MagicMock()
        llm.complete = AsyncMock(side_effect=[mock_light, mock_heavy])
        llm.extract_text = MagicMock(return_value=uncertain_text)
        llm.extract_model_name = MagicMock(side_effect=["light-model", "heavy-model"])

        response, model = await _call_with_escalation(
            llm_client=llm,
            messages=[{"role": "user", "content": "Complex question"}],
            model_light="light-model",
            model_heavy="heavy-model",
        )
        assert model == "heavy-model"
        assert llm.complete.call_count == 2

    async def test_no_escalation_for_long_uncertain_response(self):
        """Long response with uncertainty marker should NOT escalate.

        Uncertainty + short length together trigger escalation.
        A long response is accepted even if it contains uncertainty phrases.
        """
        long_uncertain = "I'm not sure, but " + ("here is extensive detail. " * 20)
        assert len(long_uncertain) >= _ESCALATION_MIN_LENGTH

        llm = _make_llm(long_uncertain, "light-model")

        _, model = await _call_with_escalation(
            llm_client=llm,
            messages=[{"role": "user", "content": "Question"}],
            model_light="light-model",
            model_heavy="heavy-model",
        )
        assert model == "light-model"
        assert llm.complete.call_count == 1

    async def test_no_escalation_for_short_confident_response(self):
        """Short response without uncertainty markers should NOT escalate."""
        short_confident = "42"
        assert len(short_confident) < _ESCALATION_MIN_LENGTH

        llm = _make_llm(short_confident, "light-model")

        _, model = await _call_with_escalation(
            llm_client=llm,
            messages=[{"role": "user", "content": "What is 6*7?"}],
            model_light="light-model",
            model_heavy="heavy-model",
        )
        assert model == "light-model"
        assert llm.complete.call_count == 1

    async def test_escalates_on_i_dont_know(self):
        llm = MagicMock()
        mock_response = MagicMock()
        llm.complete = AsyncMock(return_value=mock_response)
        llm.extract_text = MagicMock(return_value="I don't know the answer.")
        llm.extract_model_name = MagicMock(side_effect=["light-model", "heavy-model"])

        _, model = await _call_with_escalation(
            llm_client=llm,
            messages=[{"role": "user", "content": "Hard question"}],
            model_light="light-model",
            model_heavy="heavy-model",
        )
        assert model == "heavy-model"

    async def test_escalates_on_cannot_determine(self):
        llm = MagicMock()
        mock_response = MagicMock()
        llm.complete = AsyncMock(return_value=mock_response)
        llm.extract_text = MagicMock(return_value="Cannot determine the result.")
        llm.extract_model_name = MagicMock(side_effect=["light-model", "heavy-model"])

        _, model = await _call_with_escalation(
            llm_client=llm,
            messages=[{"role": "user", "content": "Hard question"}],
            model_light="light-model",
            model_heavy="heavy-model",
        )
        assert model == "heavy-model"

    async def test_escalates_max_once(self):
        """Even with multiple uncertainty markers, only escalates once."""
        uncertain_text = "I'm not sure, unclear, cannot determine this."
        assert len(uncertain_text) < _ESCALATION_MIN_LENGTH

        llm = MagicMock()
        mock_response = MagicMock()
        llm.complete = AsyncMock(return_value=mock_response)
        llm.extract_text = MagicMock(return_value=uncertain_text)
        llm.extract_model_name = MagicMock(side_effect=["light-model", "heavy-model"])

        _, model = await _call_with_escalation(
            llm_client=llm,
            messages=[{"role": "user", "content": "Hard question"}],
            model_light="light-model",
            model_heavy="heavy-model",
        )
        # No more than 2 calls (light + heavy)
        assert llm.complete.call_count == 2

    async def test_returns_response_object(self):
        confident_text = "A definitive answer with details. " * 10
        mock_response = MagicMock()
        llm = MagicMock()
        llm.complete = AsyncMock(return_value=mock_response)
        llm.extract_text = MagicMock(return_value=confident_text)
        llm.extract_model_name = MagicMock(return_value="light-model")

        response, _ = await _call_with_escalation(
            llm_client=llm,
            messages=[{"role": "user", "content": "Question"}],
            model_light="light-model",
            model_heavy="heavy-model",
        )
        assert response is mock_response
