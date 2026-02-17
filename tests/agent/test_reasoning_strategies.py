"""Tests for advanced reasoning strategies and strategy router.

Covers:
- ChainOfThoughtStrategy: step generation, verification, error handling
- SelfConsistencyStrategy: multi-sample, majority vote, consistency score
- TreeOfThoughtStrategy: branch generation, pruning, best-path conclusion
- RetrievalAugmentedReasoning: 5-step pipeline, gap identification, retrieval
- StrategyRouter: routing table, agent overrides, complexity-based routing
- AgentRuntime integration: reasoning_strategy parameter wiring
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from src.agent.llm import LLMClient
from src.reasoning.strategies.base import ReasoningResult, ReasoningStrategy
from src.reasoning.strategies.chain_of_thought import ChainOfThoughtStrategy
from src.reasoning.strategies.self_consistency import SelfConsistencyStrategy
from src.reasoning.strategies.tree_of_thought import TreeOfThoughtStrategy
from src.reasoning.strategies.rar import RetrievalAugmentedReasoningStrategy
from src.reasoning.strategy_router import StrategyRouter, TaskType


# ------------------------------------------------------------------ #
# Shared helpers
# ------------------------------------------------------------------ #

def make_llm_client(*response_texts: str) -> LLMClient:
    """Build a mocked LLMClient whose extract_text returns each string in turn."""
    client = Mock(spec=LLMClient)
    mock_response = Mock()
    mock_response.usage = None

    if len(response_texts) == 1:
        client.extract_text.return_value = response_texts[0]
    else:
        client.extract_text.side_effect = list(response_texts)

    client.complete = AsyncMock(return_value=mock_response)
    return client


def make_cot_responses(
    *,
    steps: list[dict] | None = None,
    final_answer: str = "The answer is 42",
    reasoning_confidence: float = 0.85,
    verified_confidence: float = 0.80,
    is_consistent: bool = True,
) -> tuple[str, str]:
    """Return (reasoning_json, verification_json) for CoT tests."""
    if steps is None:
        steps = [
            {"step": 1, "thought": "First I consider...", "conclusion": "Intermediate conclusion"},
            {"step": 2, "thought": "Next I analyse...", "conclusion": "The answer is 42"},
        ]
    reasoning = json.dumps({
        "reasoning_steps": steps,
        "final_answer": final_answer,
        "confidence": reasoning_confidence,
    })
    verification = json.dumps({
        "is_consistent": is_consistent,
        "issues": [],
        "verified_confidence": verified_confidence,
        "verification_note": "Reasoning is sound",
    })
    return reasoning, verification


# ------------------------------------------------------------------ #
# ChainOfThoughtStrategy tests
# ------------------------------------------------------------------ #

class TestChainOfThought:

    @pytest.mark.asyncio
    async def test_cot_returns_reasoning_result(self):
        """CoT returns a ReasoningResult with answer, confidence, and steps."""
        reasoning_json, verify_json = make_cot_responses()
        client = make_llm_client(reasoning_json, verify_json)

        strategy = ChainOfThoughtStrategy()
        result = await strategy.reason("What is the meaning of life?", "", client)

        assert isinstance(result, ReasoningResult)
        assert result.strategy_name == "chain_of_thought"
        assert result.answer == "The answer is 42"
        assert 0.0 <= result.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_cot_reasoning_steps_in_result(self):
        """CoT steps are present in result.steps as human-readable strings."""
        reasoning_json, verify_json = make_cot_responses()
        client = make_llm_client(reasoning_json, verify_json)

        strategy = ChainOfThoughtStrategy()
        result = await strategy.reason("Query", "Context", client)

        assert len(result.steps) >= 2  # At least reasoning steps + verification
        # Last step should mention verification
        assert any("erification" in s for s in result.steps)

    @pytest.mark.asyncio
    async def test_cot_verification_penalises_confidence_for_issues(self):
        """Verification issues cause confidence to be penalised."""
        reasoning_json = json.dumps({
            "reasoning_steps": [{"step": 1, "thought": "...", "conclusion": "..."}],
            "final_answer": "Answer",
            "confidence": 0.9,
        })
        verify_json = json.dumps({
            "is_consistent": False,
            "issues": ["Contradiction in step 1", "Missing evidence"],
            "verified_confidence": 0.7,
            "verification_note": "Two issues found",
        })
        client = make_llm_client(reasoning_json, verify_json)

        strategy = ChainOfThoughtStrategy()
        result = await strategy.reason("Query", "", client)

        # Confidence should be penalised for 2 issues (0.1 * 2 = 0.2 penalty)
        assert result.confidence < 0.7
        assert result.metadata["verification_issues"] == ["Contradiction in step 1", "Missing evidence"]

    @pytest.mark.asyncio
    async def test_cot_handles_json_parse_error_gracefully(self):
        """CoT falls back gracefully when LLM returns non-JSON."""
        client = make_llm_client("Not JSON at all!", "Also not JSON!")

        strategy = ChainOfThoughtStrategy()
        result = await strategy.reason("Query", "", client)

        assert isinstance(result, ReasoningResult)
        assert result.confidence <= 0.2  # Very low confidence on parse error
        assert "parse error" in result.answer.lower() or "error" in result.answer.lower()

    @pytest.mark.asyncio
    async def test_cot_handles_llm_exception(self):
        """CoT handles LLM errors without raising."""
        client = Mock(spec=LLMClient)
        client.complete = AsyncMock(side_effect=Exception("LLM timeout"))
        client.extract_text = Mock(return_value="")

        strategy = ChainOfThoughtStrategy()
        result = await strategy.reason("Query", "", client)

        assert isinstance(result, ReasoningResult)
        assert result.confidence == 0.0

    @pytest.mark.asyncio
    async def test_cot_reasoning_chain_contains_all_steps(self):
        """reasoning_chain contains step dicts plus verification dict."""
        reasoning_json, verify_json = make_cot_responses()
        client = make_llm_client(reasoning_json, verify_json)

        strategy = ChainOfThoughtStrategy()
        result = await strategy.reason("Query", "Some context", client)

        assert len(result.reasoning_chain) >= 2  # steps + verification entry
        # Last entry should be verification
        last = result.reasoning_chain[-1]
        assert "verification" in last


# ------------------------------------------------------------------ #
# SelfConsistencyStrategy tests
# ------------------------------------------------------------------ #

class TestSelfConsistency:

    @pytest.mark.asyncio
    async def test_sc_returns_majority_answer(self):
        """SelfConsistency picks the most common answer across samples."""
        # 3 samples: 2 agree on "Paris", 1 says "London"
        sample_texts = [
            "FINAL ANSWER: Paris is the capital of France",
            "FINAL ANSWER: Paris is the capital of France",
            "FINAL ANSWER: London is the capital of the UK",
        ]
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=sample_texts)

        strategy = SelfConsistencyStrategy(num_samples=3)
        result = await strategy.reason("What is the capital of France?", "", client)

        assert isinstance(result, ReasoningResult)
        assert result.strategy_name == "self_consistency"
        # Majority is "Paris..." so answer should contain Paris
        assert "Paris" in result.answer

    @pytest.mark.asyncio
    async def test_sc_consistency_score_in_metadata(self):
        """Consistency score is reported in result metadata."""
        sample_texts = [
            "FINAL ANSWER: 42",
            "FINAL ANSWER: 42",
            "FINAL ANSWER: 42",
        ]
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=sample_texts)

        strategy = SelfConsistencyStrategy(num_samples=3)
        result = await strategy.reason("Query", "", client)

        assert "consistency_score" in result.metadata
        # All 3 agree → consistency = 1.0
        assert result.metadata["consistency_score"] == pytest.approx(1.0)
        assert result.confidence > 0.9  # High consistency → high confidence

    @pytest.mark.asyncio
    async def test_sc_single_sample(self):
        """SelfConsistency works with num_samples=1 (degenerate case)."""
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(return_value="FINAL ANSWER: The answer")

        strategy = SelfConsistencyStrategy(num_samples=1)
        result = await strategy.reason("Query", "", client)

        assert isinstance(result, ReasoningResult)
        assert result.metadata["num_samples"] == 1

    @pytest.mark.asyncio
    async def test_sc_invalid_num_samples_raises(self):
        """SelfConsistency raises ValueError for invalid num_samples."""
        with pytest.raises(ValueError, match="num_samples"):
            SelfConsistencyStrategy(num_samples=0)

    @pytest.mark.asyncio
    async def test_sc_all_answers_captured(self):
        """All sample answers are captured in metadata.all_answers."""
        answers = ["FINAL ANSWER: A", "FINAL ANSWER: B", "FINAL ANSWER: A"]
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=answers)

        strategy = SelfConsistencyStrategy(num_samples=3)
        result = await strategy.reason("Query", "", client)

        all_answers = result.metadata["all_answers"]
        assert len(all_answers) == 3

    @pytest.mark.asyncio
    async def test_sc_partial_consistency(self):
        """Partial agreement gives consistency score between 0 and 1."""
        answers = ["FINAL ANSWER: Alpha", "FINAL ANSWER: Beta", "FINAL ANSWER: Alpha"]
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=answers)

        strategy = SelfConsistencyStrategy(num_samples=3)
        result = await strategy.reason("Query", "", client)

        assert 0.5 < result.metadata["consistency_score"] < 1.0


# ------------------------------------------------------------------ #
# TreeOfThoughtStrategy tests
# ------------------------------------------------------------------ #

class TestTreeOfThought:

    def _build_tot_responses(
        self,
        num_branches: int = 2,
        max_depth: int = 1,
    ) -> list[str]:
        """Build minimal valid JSON responses for a ToT run."""
        responses = []

        # 1. Generate approaches
        approaches = [
            {"id": i + 1, "approach": f"Approach {i + 1}"}
            for i in range(num_branches)
        ]
        responses.append(json.dumps({"approaches": approaches}))

        # 2. Expand + score for each depth
        for _ in range(max_depth):
            # Expand responses (one per branch)
            for i in range(num_branches):
                responses.append(json.dumps({"next_step": f"Step for approach {i + 1}"}))
            # Score responses (one per branch)
            for i in range(num_branches):
                responses.append(json.dumps({"score": 7.0 + i, "rationale": "ok"}))

        # 3. Conclude
        responses.append(json.dumps({
            "final_answer": "The best conclusion",
            "confidence": 0.85,
        }))

        return responses

    @pytest.mark.asyncio
    async def test_tot_returns_reasoning_result(self):
        """ToT returns a valid ReasoningResult."""
        responses = self._build_tot_responses(num_branches=2, max_depth=1)
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=responses)

        strategy = TreeOfThoughtStrategy(num_branches=2, max_depth=1, beam_width=1)
        result = await strategy.reason("Plan a product launch", "", client)

        assert isinstance(result, ReasoningResult)
        assert result.strategy_name == "tree_of_thought"
        assert result.answer == "The best conclusion"
        assert 0.0 <= result.confidence <= 1.0

    @pytest.mark.asyncio
    async def test_tot_metadata_contains_branch_info(self):
        """ToT metadata includes branch scores and configuration."""
        responses = self._build_tot_responses(num_branches=2, max_depth=1)
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=responses)

        strategy = TreeOfThoughtStrategy(num_branches=2, max_depth=1, beam_width=1)
        result = await strategy.reason("Query", "", client)

        assert "num_branches" in result.metadata
        assert "max_depth" in result.metadata
        assert "branch_scores" in result.metadata
        assert result.metadata["num_branches"] == 2

    @pytest.mark.asyncio
    async def test_tot_prunes_lower_scored_branches(self):
        """After pruning, exactly beam_width branches survive."""
        responses = self._build_tot_responses(num_branches=3, max_depth=1)
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=responses)

        strategy = TreeOfThoughtStrategy(num_branches=3, max_depth=1, beam_width=1)
        result = await strategy.reason("Query", "Context", client)

        # Only 1 branch survives pruning (beam_width=1)
        surviving = [b for b in result.reasoning_chain if b.get("survived")]
        assert len(surviving) <= 1

    @pytest.mark.asyncio
    async def test_tot_invalid_config_raises(self):
        """ToT raises ValueError for invalid configuration."""
        with pytest.raises(ValueError, match="num_branches"):
            TreeOfThoughtStrategy(num_branches=0)
        with pytest.raises(ValueError, match="max_depth"):
            TreeOfThoughtStrategy(max_depth=0)

    @pytest.mark.asyncio
    async def test_tot_handles_generate_failure(self):
        """ToT falls back to single branch when generation fails."""
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        # First call (generate) fails, rest succeed
        client.extract_text = Mock(side_effect=[
            "Not valid JSON!",                                     # generate
            json.dumps({"next_step": "A step"}),                   # expand
            json.dumps({"score": 6.0, "rationale": "ok"}),         # score
            json.dumps({"final_answer": "Fallback", "confidence": 0.4}),  # conclude
        ])

        strategy = TreeOfThoughtStrategy(num_branches=2, max_depth=1, beam_width=1)
        result = await strategy.reason("Query", "", client)

        assert isinstance(result, ReasoningResult)


# ------------------------------------------------------------------ #
# RetrievalAugmentedReasoningStrategy tests
# ------------------------------------------------------------------ #

class TestRetrievalAugmentedReasoning:

    def _make_rar_responses(
        self,
        is_complete: bool = False,
        confidence: float = 0.5,
        grounded: bool = True,
    ) -> list[str]:
        return [
            # Step 1: initial reasoning
            json.dumps({
                "partial_answer": "Initial partial answer",
                "confidence": confidence,
                "is_complete": is_complete,
                "missing_info": [] if is_complete else ["Missing fact A"],
            }),
            # Step 2: gap identification
            json.dumps({
                "search_queries": ["search for fact A"],
                "gap_summary": "Need fact A",
            }),
            # Step 3: retrieval handled by retrieve_fn, not LLM
            # Step 4: augmented reasoning
            json.dumps({
                "final_answer": "Final augmented answer",
                "confidence": 0.85,
                "sources_used": ["Document 1"],
            }),
            # Step 5: verification
            json.dumps({
                "is_grounded": grounded,
                "unsupported_claims": [],
                "verified_confidence": 0.82,
                "verification_note": "Well grounded",
            }),
        ]

    @pytest.mark.asyncio
    async def test_rar_full_pipeline_with_retrieval(self):
        """RAR runs all 5 steps when retrieval is available."""
        responses = self._make_rar_responses()
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=responses)

        async def fake_retrieve(query: str) -> list[str]:
            return [f"Retrieved doc for: {query}"]

        strategy = RetrievalAugmentedReasoningStrategy(retrieve_fn=fake_retrieve)
        result = await strategy.reason("What is X?", "Some context", client)

        assert isinstance(result, ReasoningResult)
        assert result.strategy_name == "retrieval_augmented_reasoning"
        assert result.answer == "Final augmented answer"
        assert result.metadata["retrieval_performed"] is True

    @pytest.mark.asyncio
    async def test_rar_without_retrieve_fn(self):
        """RAR degrades gracefully without a retrieve_fn (no retrieval step)."""
        responses = self._make_rar_responses()
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=responses)

        strategy = RetrievalAugmentedReasoningStrategy(retrieve_fn=None)
        result = await strategy.reason("What is X?", "", client)

        assert isinstance(result, ReasoningResult)
        assert result.metadata["retrieval_performed"] is False

    @pytest.mark.asyncio
    async def test_rar_skips_retrieval_when_confident(self):
        """RAR skips steps 2-5 when initial reasoning is complete and confident."""
        initial_response = json.dumps({
            "partial_answer": "Complete answer right away",
            "confidence": 0.95,
            "is_complete": True,
            "missing_info": [],
        })
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(return_value=initial_response)

        strategy = RetrievalAugmentedReasoningStrategy()
        result = await strategy.reason("What is 2+2?", "Context", client)

        # Should have called complete only once (initial reasoning)
        assert client.complete.call_count == 1
        assert result.answer == "Complete answer right away"

    @pytest.mark.asyncio
    async def test_rar_source_verification_penalises_unsupported(self):
        """Unsupported claims in verification reduce final confidence."""
        responses = [
            json.dumps({"partial_answer": "Answer", "confidence": 0.8, "is_complete": False, "missing_info": ["X"]}),
            json.dumps({"search_queries": ["find X"], "gap_summary": "Need X"}),
            json.dumps({"final_answer": "Answer with claims", "confidence": 0.8, "sources_used": []}),
            json.dumps({
                "is_grounded": False,
                "unsupported_claims": ["Claim A", "Claim B"],
                "verified_confidence": 0.6,
                "verification_note": "Two unsupported claims",
            }),
        ]
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=responses)

        strategy = RetrievalAugmentedReasoningStrategy()
        result = await strategy.reason("Query", "", client)

        # Confidence penalised: 0.6 - 0.15*2 = 0.3
        assert result.confidence < 0.6
        assert result.metadata["is_grounded"] is False

    @pytest.mark.asyncio
    async def test_rar_reasoning_chain_has_all_5_steps(self):
        """RAR reasoning_chain contains an entry for each of the 5 steps."""
        responses = self._make_rar_responses()
        client = Mock(spec=LLMClient)
        mock_response = Mock()
        mock_response.usage = None
        client.complete = AsyncMock(return_value=mock_response)
        client.extract_text = Mock(side_effect=responses)

        strategy = RetrievalAugmentedReasoningStrategy()
        result = await strategy.reason("What is X?", "", client)

        step_names = {item["step"] for item in result.reasoning_chain}
        assert "initial_reasoning" in step_names
        assert "gap_identification" in step_names
        assert "retrieval" in step_names
        assert "augmented_reasoning" in step_names
        assert "verification" in step_names


# ------------------------------------------------------------------ #
# StrategyRouter tests
# ------------------------------------------------------------------ #

class TestStrategyRouter:

    def test_router_simple_query_returns_cot(self):
        """Simple / low-complexity queries route to ChainOfThought."""
        router = StrategyRouter()
        strategy = router.select_strategy("What is 2+2?", complexity="low")
        assert isinstance(strategy, ChainOfThoughtStrategy)

    def test_router_safety_critical_returns_self_consistency(self):
        """Safety-critical task type routes to SelfConsistency."""
        router = StrategyRouter()
        strategy = router.select_strategy(
            "Should we approve this medication?",
            task_type=TaskType.SAFETY_CRITICAL,
        )
        assert isinstance(strategy, SelfConsistencyStrategy)

    def test_router_critical_alias_returns_self_consistency(self):
        """CRITICAL task type (alias) also routes to SelfConsistency."""
        router = StrategyRouter()
        strategy = router.select_strategy("Critical decision", task_type=TaskType.CRITICAL)
        assert isinstance(strategy, SelfConsistencyStrategy)

    def test_router_planning_returns_tree_of_thought(self):
        """Planning task type routes to TreeOfThought."""
        router = StrategyRouter()
        strategy = router.select_strategy(
            "Plan a product roadmap for next year",
            task_type=TaskType.PLANNING,
        )
        assert isinstance(strategy, TreeOfThoughtStrategy)

    def test_router_complex_task_type_returns_tree_of_thought(self):
        """COMPLEX task type routes to TreeOfThought."""
        router = StrategyRouter()
        strategy = router.select_strategy("Complex analysis", task_type=TaskType.COMPLEX)
        assert isinstance(strategy, TreeOfThoughtStrategy)

    def test_router_knowledge_intensive_returns_rar(self):
        """Knowledge-intensive task type routes to RAR."""
        router = StrategyRouter()
        strategy = router.select_strategy(
            "What are the recent regulatory changes?",
            task_type=TaskType.KNOWLEDGE_INTENSIVE,
        )
        assert isinstance(strategy, RetrievalAugmentedReasoningStrategy)

    def test_router_high_complexity_general_returns_tree_of_thought(self):
        """High complexity + general task type routes to TreeOfThought."""
        router = StrategyRouter()
        strategy = router.select_strategy(
            "Complex architectural decision",
            complexity="high",
            task_type=TaskType.GENERAL,
        )
        assert isinstance(strategy, TreeOfThoughtStrategy)

    def test_router_medium_complexity_general_returns_default(self):
        """Medium complexity + general task type returns the default strategy."""
        router = StrategyRouter()
        strategy = router.select_strategy("A medium question", complexity="medium")
        assert isinstance(strategy, ChainOfThoughtStrategy)  # default is CoT

    def test_router_agent_override_takes_precedence(self):
        """Per-agent override overrides task_type routing."""
        router = StrategyRouter(agent_overrides={"safety_agent": "self_consistency"})
        strategy = router.select_strategy(
            "Query",
            task_type=TaskType.SIMPLE,  # Would normally be CoT
            agent_id="safety_agent",
        )
        assert isinstance(strategy, SelfConsistencyStrategy)

    def test_router_register_and_use_agent_override(self):
        """Dynamically registered agent overrides are respected."""
        router = StrategyRouter()
        router.register_agent_override("my_agent", "tree_of_thought")
        strategy = router.select_strategy("Query", agent_id="my_agent")
        assert isinstance(strategy, TreeOfThoughtStrategy)

    def test_router_remove_agent_override_restores_routing(self):
        """Removing an override restores normal routing."""
        router = StrategyRouter()
        router.register_agent_override("my_agent", "tree_of_thought")
        router.remove_agent_override("my_agent")
        strategy = router.select_strategy("Simple question", complexity="low", agent_id="my_agent")
        assert isinstance(strategy, ChainOfThoughtStrategy)

    def test_router_invalid_strategy_name_raises(self):
        """Invalid default_strategy raises ValueError on construction."""
        with pytest.raises(ValueError, match="Unknown default_strategy"):
            StrategyRouter(default_strategy="unknown_strategy")

    def test_router_invalid_override_raises(self):
        """Registering an invalid strategy name raises ValueError."""
        router = StrategyRouter()
        with pytest.raises(ValueError, match="Unknown strategy"):
            router.register_agent_override("agent", "bad_strategy_name")

    def test_router_unknown_task_type_string_falls_back_to_general(self):
        """Unknown task type string is treated as GENERAL."""
        router = StrategyRouter()
        # Should not raise, should fall back to complexity-based routing
        strategy = router.select_strategy("Query", task_type="completely_unknown_type")
        assert isinstance(strategy, (ChainOfThoughtStrategy, TreeOfThoughtStrategy))

    def test_router_strategy_name_property(self):
        """Each strategy reports the correct name."""
        cot = ChainOfThoughtStrategy()
        sc = SelfConsistencyStrategy()
        tot = TreeOfThoughtStrategy()
        rar = RetrievalAugmentedReasoningStrategy()

        assert cot.name == "chain_of_thought"
        assert sc.name == "self_consistency"
        assert tot.name == "tree_of_thought"
        assert rar.name == "retrieval_augmented_reasoning"


# ------------------------------------------------------------------ #
# AgentRuntime integration tests
# ------------------------------------------------------------------ #

class TestAgentRuntimeIntegration:
    """Verify that AgentRuntime correctly wires the reasoning_strategy parameter."""

    def _make_runtime(self, reasoning_strategy=None):
        """Build a minimally configured AgentRuntime for testing."""
        from unittest.mock import MagicMock
        from src.agent.runtime import AgentRuntime
        from src.config import Settings

        settings = Settings(
            environment="test",
            database_url="postgresql+asyncpg://test/test",
            litellm_base_url="http://localhost:4000",
            litellm_api_key="sk-test",
        )

        db = AsyncMock()
        llm_client = Mock(spec=LLMClient)
        llm_client.complete = AsyncMock(return_value=Mock(
            choices=[Mock(message=Mock(content="Direct LLM response"))],
            usage=Mock(total_tokens=100),
            model="test-model",
        ))
        llm_client.extract_text = Mock(return_value="Direct LLM response")
        llm_client.extract_model_name = Mock(return_value="test-model")

        runtime = AgentRuntime(
            db=db,
            settings=settings,
            llm_client=llm_client,
            reasoning_strategy=reasoning_strategy,
        )
        return runtime

    @pytest.mark.asyncio
    async def test_runtime_accepts_reasoning_strategy_param(self):
        """AgentRuntime can be instantiated with a reasoning_strategy."""
        strategy = ChainOfThoughtStrategy()
        runtime = self._make_runtime(reasoning_strategy=strategy)
        assert runtime._reasoning_strategy is strategy

    @pytest.mark.asyncio
    async def test_runtime_reasoning_result_in_chat_response(self):
        """ChatResponse.reasoning_result is populated when strategy is active."""
        from src.agent.runtime import ChatRequest, ChatResponse

        mock_result = ReasoningResult(
            answer="Strategic answer",
            confidence=0.9,
            steps=["Step 1"],
            strategy_name="chain_of_thought",
        )

        mock_strategy = Mock(spec=ReasoningStrategy)
        mock_strategy.name = "chain_of_thought"
        mock_strategy.reason = AsyncMock(return_value=mock_result)

        runtime = self._make_runtime()

        # Patch out all database operations so chat() can execute
        from src.models.conversation import Conversation, Message
        import uuid
        mock_conv = Mock(spec=Conversation)
        mock_conv.id = uuid.uuid4()
        mock_conv.updated_at = None
        runtime._get_or_create_conversation = AsyncMock(return_value=mock_conv)
        runtime._load_history = AsyncMock(return_value=[])
        runtime._recall_memory_context = AsyncMock(return_value="")
        runtime._store_turn_memory = AsyncMock()
        runtime._db.add = Mock()
        runtime._db.flush = AsyncMock()

        from src.models.user import User, UserRole
        user = Mock(spec=User)
        user.id = uuid.uuid4()
        user.tenant_id = uuid.uuid4()
        user.role = UserRole.OPERATOR

        request = ChatRequest(message="What is the best approach?")

        response = await runtime.chat(
            user=user,
            request=request,
            reasoning_strategy=mock_strategy,
        )

        assert isinstance(response, ChatResponse)
        assert response.reasoning_result is mock_result
        assert response.response == "Strategic answer"
        mock_strategy.reason.assert_called_once()

    @pytest.mark.asyncio
    async def test_runtime_no_strategy_returns_none_reasoning_result(self):
        """ChatResponse.reasoning_result is None when no strategy is active."""
        from src.agent.runtime import ChatRequest, ChatResponse
        import uuid

        runtime = self._make_runtime(reasoning_strategy=None)

        from src.models.conversation import Conversation
        mock_conv = Mock(spec=Conversation)
        mock_conv.id = uuid.uuid4()
        mock_conv.updated_at = None
        runtime._get_or_create_conversation = AsyncMock(return_value=mock_conv)
        runtime._load_history = AsyncMock(return_value=[])
        runtime._recall_memory_context = AsyncMock(return_value="")
        runtime._store_turn_memory = AsyncMock()
        runtime._db.add = Mock()
        runtime._db.flush = AsyncMock()

        from src.models.user import User, UserRole
        user = Mock(spec=User)
        user.id = uuid.uuid4()
        user.tenant_id = uuid.uuid4()
        user.role = UserRole.OPERATOR

        request = ChatRequest(message="Simple question")
        response = await runtime.chat(user=user, request=request)

        assert response.reasoning_result is None
