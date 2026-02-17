"""Tests for thinking tools: RedTeam, Council, FirstPrinciples.

Tests cover:
- RedTeam generates adversarial perspectives and returns ThinkingToolOutput
- Council creates multiple perspectives and synthesizes
- FirstPrinciples decomposes problem into fundamentals
- ThinkingToolOutput aggregation
- Mock all LLM calls
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, Mock

import pytest

from src.agent.llm import LLMClient
from src.agent.thinking import ThinkingToolOutput
from src.agent.thinking.council import Council, CouncilResult, Perspective
from src.agent.thinking.first_principles import FirstPrinciples, FirstPrinciplesResult, PrincipleNode
from src.agent.thinking.red_team import AdversarialFinding, RedTeam, RedTeamResult, Severity


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #


@pytest.fixture
def mock_llm_client():
    """Mock LLM client for testing."""
    client = Mock(spec=LLMClient)
    client.complete = AsyncMock()
    client.extract_text = Mock()
    return client


# ------------------------------------------------------------------ #
# RedTeam tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_red_team_no_findings(mock_llm_client):
    """Test RedTeam analysis with no findings (clean response)."""
    red_team = RedTeam(llm_client=mock_llm_client)

    # Mock all 4 parallel checks to return no findings
    check_responses = [json.dumps({"findings": []})] * 4

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = check_responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await red_team.analyze(
        response="This is a clean, well-grounded response.",
        sources=["Source document with supporting evidence"],
        clearance="class_ii",
        query="What is X?",
    )

    assert isinstance(result, RedTeamResult)
    assert len(result.findings) == 0
    assert result.overall_severity == Severity.LOW
    assert result.requires_human_review is False
    assert result.overall_confidence == 1.0


@pytest.mark.asyncio
async def test_red_team_medium_findings(mock_llm_client):
    """Test RedTeam analysis with medium severity findings."""
    red_team = RedTeam(llm_client=mock_llm_client)

    # Mock checks: one with medium finding, rest empty, then aggregation
    check_responses = [
        json.dumps({"findings": []}),  # Factual check
        json.dumps({  # Safety check with medium finding
            "findings": [
                {
                    "severity": "medium",
                    "description": "Missing safety warning",
                    "evidence": ["No mention of protective equipment"],
                    "recommendation": "Add PPE requirement",
                }
            ]
        }),
        json.dumps({"findings": []}),  # Confidence check
        json.dumps({"findings": []}),  # Classification check
        json.dumps({  # Aggregation response
            "requires_human_review": False,
            "overall_confidence": 0.8,
            "review_reason": None,
        }),
    ]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = check_responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await red_team.analyze(
        response="Do task X without mentioning safety.",
        sources=["Source"],
        clearance="class_ii",
        query="How to do X?",
    )

    assert len(result.findings) == 1
    assert result.findings[0].severity == Severity.MEDIUM
    assert result.findings[0].category == "safety_omissions"
    assert result.overall_confidence == 0.8
    assert result.requires_human_review is False


@pytest.mark.asyncio
async def test_red_team_critical_findings_block_response(mock_llm_client):
    """Test RedTeam analysis with critical findings blocks response."""
    red_team = RedTeam(llm_client=mock_llm_client)

    # Mock check with critical finding
    check_responses = [
        json.dumps({  # Factual check with critical finding
            "findings": [
                {
                    "severity": "critical",
                    "description": "Contradicts source material",
                    "evidence": ["Response says X, source says Y"],
                    "recommendation": "Correct factual error",
                }
            ]
        }),
        json.dumps({"findings": []}),
        json.dumps({"findings": []}),
        json.dumps({"findings": []}),
        # No aggregation needed - critical findings block automatically
    ]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = check_responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await red_team.analyze(
        response="Incorrect statement",
        sources=["Correct information"],
        clearance="class_ii",
    )

    assert len(result.findings) == 1
    assert result.overall_severity == Severity.CRITICAL
    assert result.requires_human_review is True
    assert result.overall_confidence == 0.2
    assert "CRITICAL" in result.review_reason


@pytest.mark.asyncio
async def test_red_team_handles_check_failure(mock_llm_client):
    """Test RedTeam handles individual check failures gracefully."""
    red_team = RedTeam(llm_client=mock_llm_client)

    # First check raises exception, rest succeed
    async def mock_complete_with_error(*args, **kwargs):
        call_count = mock_complete_with_error.call_count
        mock_complete_with_error.call_count += 1

        if call_count == 0:
            raise Exception("LLM timeout")
        return Mock()

    mock_complete_with_error.call_count = 0
    mock_llm_client.complete.side_effect = mock_complete_with_error

    # Responses for successful checks
    mock_llm_client.extract_text.side_effect = [
        json.dumps({"findings": []}),
        json.dumps({"findings": []}),
        json.dumps({"findings": []}),
        json.dumps({
            "requires_human_review": True,
            "overall_confidence": 0.6,
            "review_reason": "Check failure",
        }),
    ]

    result = await red_team.analyze(
        response="Response",
        sources=["Source"],
        clearance="class_ii",
    )

    # Should have one system_error finding from the failed check
    system_errors = [f for f in result.findings if f.category == "system_error"]
    assert len(system_errors) == 1
    assert system_errors[0].severity == Severity.HIGH


# ------------------------------------------------------------------ #
# Council tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_council_generates_perspectives(mock_llm_client):
    """Test Council generates multiple perspectives."""
    council = Council(llm_client=mock_llm_client)

    # Mock responses for 3 perspectives + 3 critiques + 1 synthesis
    perspective_responses = [
        json.dumps({
            "position": "Pragmatic: Ship quickly with MVP",
            "arguments": ["Speed to market", "Learn from users"],
            "confidence": 0.7,
        }),
        json.dumps({
            "position": "Quality: Build it right from the start",
            "arguments": ["Technical debt is expensive", "Maintainability"],
            "confidence": 0.8,
        }),
        json.dumps({
            "position": "Risk: Identify failure modes first",
            "arguments": ["Prevent outages", "Security review needed"],
            "confidence": 0.75,
        }),
    ]

    critique_responses = [
        json.dumps({"critiques": ["Pragmatic critique 1", "Pragmatic critique 2"]}),
        json.dumps({"critiques": ["Quality critique 1"]}),
        json.dumps({"critiques": ["Risk critique 1"]}),
    ]

    synthesis_response = json.dumps({
        "consensus": "Balanced approach: MVP with quality gates",
        "confidence": 0.8,
        "dissenting_views": [],
        "requires_review": False,
        "review_reason": None,
    })

    all_responses = perspective_responses + critique_responses + [synthesis_response]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = all_responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await council.deliberate(
        query="Should we use approach X or Y?",
        context="We need to decide...",
    )

    assert isinstance(result, CouncilResult)
    assert len(result.perspectives) == 3
    assert "Pragmatic" in result.perspectives[0].name
    assert result.perspectives[0].critiques is not None
    assert len(result.perspectives[0].critiques) == 2
    assert "Balanced approach" in result.consensus
    assert result.requires_human_review is False


@pytest.mark.asyncio
async def test_council_flags_deep_conflicts(mock_llm_client):
    """Test Council flags deep conflicts for human review."""
    council = Council(llm_client=mock_llm_client)

    # Mock responses with conflicting perspectives
    perspective_responses = [
        json.dumps({"position": "Position A", "arguments": ["Arg A"], "confidence": 0.9}),
        json.dumps({"position": "Position B", "arguments": ["Arg B"], "confidence": 0.9}),
        json.dumps({"position": "Position C", "arguments": ["Arg C"], "confidence": 0.9}),
    ]

    critique_responses = [json.dumps({"critiques": ["Critique"]}) for _ in range(3)]

    # Synthesis indicates irreconcilable conflict
    synthesis_response = json.dumps({
        "consensus": "No clear consensus - deep conflict",
        "confidence": 0.3,
        "dissenting_views": ["Position A", "Position B", "Position C"],
        "requires_review": True,
        "review_reason": "Perspectives deeply conflict, need human decision",
    })

    all_responses = perspective_responses + critique_responses + [synthesis_response]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = all_responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await council.deliberate(
        query="Conflicting decision",
        context="Context",
    )

    assert result.requires_human_review is True
    assert result.consensus_confidence == 0.3
    assert len(result.dissenting_views) == 3


@pytest.mark.asyncio
async def test_council_handles_synthesis_failure(mock_llm_client):
    """Test Council handles synthesis failure conservatively."""
    council = Council(llm_client=mock_llm_client)

    # Mock perspectives succeed, synthesis fails
    perspective_responses = [
        json.dumps({"position": "Pos A", "arguments": ["Arg"], "confidence": 0.8})
        for _ in range(3)
    ]

    critique_responses = [json.dumps({"critiques": []}) for _ in range(3)]

    all_responses = perspective_responses + critique_responses + ["Invalid JSON!"]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = all_responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await council.deliberate(
        query="Query",
        context="Context",
    )

    # Conservative fallback: require human review
    assert result.requires_human_review is True
    assert result.consensus_confidence == 0.3
    assert result.review_reason is not None and len(result.review_reason) > 0


# ------------------------------------------------------------------ #
# FirstPrinciples tests
# ------------------------------------------------------------------ #


@pytest.mark.asyncio
async def test_first_principles_decomposes_recursively(mock_llm_client):
    """Test FirstPrinciples decomposes query recursively."""
    fp = FirstPrinciples(llm_client=mock_llm_client)

    # Mock recursive decomposition: root -> 2 children -> leaf nodes
    responses = [
        # Root decomposition
        json.dumps({
            "answer": "Root answer",
            "is_fundamental": False,
            "assumptions": ["Assumption 1"],
            "sub_questions": ["Why sub-question 1?", "Why sub-question 2?"],
        }),
        # Sub-question 1 (leaf)
        json.dumps({
            "answer": "Fundamental answer 1",
            "is_fundamental": True,
            "assumptions": [],
            "sub_questions": [],
        }),
        # Sub-question 2 (leaf)
        json.dumps({
            "answer": "Fundamental answer 2",
            "is_fundamental": True,
            "assumptions": [],
            "sub_questions": [],
        }),
        # Synthesis
        json.dumps({
            "reconstruction": "Built from fundamental truths",
            "confidence": 0.85,
            "requires_review": False,
            "review_reason": None,
        }),
    ]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await fp.decompose(
        query="Why should we use approach X?",
        context="Context about approach X",
    )

    assert isinstance(result, FirstPrinciplesResult)
    assert result.root.question == "Why should we use approach X?"
    assert len(result.root.children) == 2
    assert len(result.fundamental_truths) == 2
    assert result.reconstruction == "Built from fundamental truths"
    assert result.reconstruction_confidence == 0.85


@pytest.mark.asyncio
async def test_first_principles_stops_at_max_depth(mock_llm_client):
    """Test FirstPrinciples stops at MAX_DEPTH."""
    fp = FirstPrinciples(llm_client=mock_llm_client)

    # Mock responses that keep requesting more depth
    # At MAX_DEPTH (4), should call _get_fundamental_answer instead
    responses = [
        # Depth 0
        json.dumps({
            "answer": "Answer 0",
            "is_fundamental": False,
            "assumptions": [],
            "sub_questions": ["Sub 1"],
        }),
        # Depth 1
        json.dumps({
            "answer": "Answer 1",
            "is_fundamental": False,
            "assumptions": [],
            "sub_questions": ["Sub 2"],
        }),
        # Depth 2
        json.dumps({
            "answer": "Answer 2",
            "is_fundamental": False,
            "assumptions": [],
            "sub_questions": ["Sub 3"],
        }),
        # Depth 3
        json.dumps({
            "answer": "Answer 3",
            "is_fundamental": False,
            "assumptions": [],
            "sub_questions": ["Sub 4"],
        }),
        # Depth 4 (MAX_DEPTH) - calls _get_fundamental_answer, returns text not JSON
        "Fundamental answer at max depth",
        # Synthesis
        json.dumps({
            "reconstruction": "Reconstruction",
            "confidence": 0.7,
            "requires_review": False,
            "review_reason": None,
        }),
    ]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await fp.decompose(
        query="Deep question",
        context="Context",
    )

    # Should have stopped at depth 4
    assert len(result.fundamental_truths) == 1
    assert result.fundamental_truths[0].depth == 4
    assert result.fundamental_truths[0].is_fundamental is True


@pytest.mark.asyncio
async def test_first_principles_handles_decomposition_error(mock_llm_client):
    """Test FirstPrinciples handles decomposition errors gracefully."""
    fp = FirstPrinciples(llm_client=mock_llm_client)

    # First call fails, synthesis handles empty fundamentals
    responses = [
        "Invalid JSON!",  # Decomposition fails
        json.dumps({  # Synthesis handles failure
            "reconstruction": "Unable to decompose",
            "confidence": 0.0,
            "requires_review": True,
            "review_reason": "Decomposition failed",
        }),
    ]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await fp.decompose(
        query="Query",
        context="Context",
    )

    # Should have fallback node
    assert result.root.is_fundamental is True
    assert "Unable to decompose" in result.root.answer


@pytest.mark.asyncio
async def test_first_principles_flags_questionable_assumptions(mock_llm_client):
    """Test FirstPrinciples flags questionable assumptions for review."""
    fp = FirstPrinciples(llm_client=mock_llm_client)

    responses = [
        # Root with questionable assumptions
        json.dumps({
            "answer": "Answer",
            "is_fundamental": True,
            "assumptions": ["Questionable assumption 1", "Questionable assumption 2"],
            "sub_questions": [],
        }),
        # Synthesis flags assumptions
        json.dumps({
            "reconstruction": "Based on questionable assumptions",
            "confidence": 0.4,
            "requires_review": True,
            "review_reason": "Fundamental assumptions are questionable",
        }),
    ]

    mock_llm_response = Mock()
    mock_llm_client.extract_text.side_effect = responses
    mock_llm_client.complete.return_value = mock_llm_response

    result = await fp.decompose(
        query="Query with assumptions",
        context="Context",
    )

    assert result.requires_human_review is True
    assert result.reconstruction_confidence == 0.4
    assert "questionable" in result.review_reason.lower()


# ------------------------------------------------------------------ #
# ThinkingToolOutput aggregation tests
# ------------------------------------------------------------------ #


def test_thinking_tool_output_any_invoked():
    """Test ThinkingToolOutput.any_invoked property."""
    # No tools invoked
    output = ThinkingToolOutput(red_team=None, council=None, first_principles=None)
    assert output.any_invoked is False

    # RedTeam invoked
    red_team_result = RedTeamResult(
        findings=[],
        overall_severity=Severity.LOW,
        requires_human_review=False,
        overall_confidence=1.0,
        review_reason=None,
    )
    output = ThinkingToolOutput(red_team=red_team_result, council=None, first_principles=None)
    assert output.any_invoked is True


def test_thinking_tool_output_requires_human_review():
    """Test ThinkingToolOutput.requires_human_review aggregates correctly."""
    # No tools require review
    red_team_result = RedTeamResult(
        findings=[],
        overall_severity=Severity.LOW,
        requires_human_review=False,
        overall_confidence=1.0,
        review_reason=None,
    )
    output = ThinkingToolOutput(red_team=red_team_result, council=None, first_principles=None)
    assert output.requires_human_review is False

    # RedTeam requires review
    red_team_result_critical = RedTeamResult(
        findings=[
            AdversarialFinding(
                category="test",
                severity=Severity.CRITICAL,
                description="Critical issue",
                evidence=[],
                recommendation="Fix",
            )
        ],
        overall_severity=Severity.CRITICAL,
        requires_human_review=True,
        overall_confidence=0.2,
        review_reason="Critical issue",
    )
    output = ThinkingToolOutput(red_team=red_team_result_critical, council=None, first_principles=None)
    assert output.requires_human_review is True


def test_thinking_tool_output_adjusted_confidence():
    """Test ThinkingToolOutput.adjusted_confidence takes minimum."""
    red_team_result = RedTeamResult(
        findings=[],
        overall_severity=Severity.LOW,
        requires_human_review=False,
        overall_confidence=0.9,
        review_reason=None,
    )

    council_result = CouncilResult(
        perspectives=[],
        consensus="Consensus",
        consensus_confidence=0.7,  # Lower confidence
        dissenting_views=[],
        requires_human_review=False,
        review_reason=None,
    )

    fp_result = FirstPrinciplesResult(
        root=PrincipleNode(question="Q", answer="A", depth=0, is_fundamental=True),
        fundamental_truths=[],
        reconstruction="Reconstruction",
        reconstruction_confidence=0.85,
        requires_human_review=False,
        review_reason=None,
    )

    output = ThinkingToolOutput(
        red_team=red_team_result,
        council=council_result,
        first_principles=fp_result,
    )

    # Should take minimum: 0.7 (council)
    assert output.adjusted_confidence == 0.7


def test_thinking_tool_output_no_tools_invoked_returns_1_0_confidence():
    """Test ThinkingToolOutput returns 1.0 confidence when no tools invoked."""
    output = ThinkingToolOutput(red_team=None, council=None, first_principles=None)
    assert output.adjusted_confidence == 1.0
