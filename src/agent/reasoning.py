"""Structured reasoning engine for agent decision-making.

Implements the OBSERVE→THINK→VERIFY loop for specialist agents that require
formal reasoning chains and verification. Used by safety-critical and quality-critical
agents (procedure_expert, quality_inspector, maintenance_advisor) per TE compliance.

The reasoning engine provides:
1. Observation extraction (key facts, assumptions, uncertainties)
2. Step-by-step reasoning chain construction
3. Verification of reasoning consistency and safety
4. Confidence scoring and human review flagging
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import structlog

from src.agent.llm import LLMClient
from src.agent.registry import AgentSpec
from src.config import Settings

log = structlog.get_logger(__name__)


@dataclass
class Observation:
    """What was observed from the input.

    This represents the OBSERVE phase output: extracting the key information,
    explicit assumptions, known uncertainties, and cited data sources from the
    user's query and available context.
    """
    key_facts: list[str]
    assumptions: list[str]
    uncertainties: list[str]
    data_sources: list[str]


@dataclass
class ReasoningStep:
    """A single step in the reasoning chain.

    Each step represents one logical inference in the THINK phase, with
    supporting evidence, the conclusion reached, and a confidence score.
    """
    step_number: int
    description: str
    evidence: list[str]
    conclusion: str
    confidence: float  # 0.0 - 1.0


@dataclass
class Verification:
    """Verification result for a reasoning chain.

    This represents the VERIFY phase output: checking the reasoning chain for
    internal consistency, safety compliance, and whether human review is needed.
    """
    is_verified: bool
    checks_passed: list[str]
    checks_failed: list[str]
    requires_human_review: bool
    review_reason: str | None


@dataclass
class SpecialistReasoningResult:
    """Complete result of a reasoning cycle.

    This bundles the outputs from all three phases (OBSERVE, THINK, VERIFY)
    into a single result that agents can include in their AgentResponse.
    """
    observation: Observation
    reasoning_steps: list[ReasoningStep]
    conclusion: str
    verification: Verification
    total_confidence: float


class ReasoningEngine:
    """Structured reasoning with OBSERVE→THINK→VERIFY loop.

    Used by specialist agents for safety-critical and quality-critical
    decisions per TE compliance requirements.

    The engine uses LLM calls with structured prompting to extract observations,
    build reasoning chains, and verify consistency. Temperature is set low (0.3)
    for deterministic reasoning.

    Example usage:
        engine = ReasoningEngine(llm_client, settings)
        result = await engine.reason(
            query="How do I calibrate sensor X?",
            context="RAG context with sensor documentation...",
            agent_spec=procedure_expert_spec,
            require_verification=True,
        )

        # Include in AgentResponse
        response.reasoning_trace = [
            f"Observed {len(result.observation.key_facts)} key facts",
            f"Built {len(result.reasoning_steps)} reasoning steps",
            f"Verification: {result.verification.is_verified}",
            f"Overall confidence: {result.total_confidence:.2f}",
        ]
    """

    def __init__(self, llm_client: LLMClient, settings: Settings) -> None:
        """Initialize the reasoning engine.

        Args:
            llm_client: LLM client for making reasoning calls
            settings: Application settings (for model preferences)
        """
        self._llm = llm_client
        self._settings = settings

    async def reason(
        self,
        *,
        query: str,
        context: str,
        agent_spec: AgentSpec,
        require_verification: bool = True,
    ) -> SpecialistReasoningResult:
        """Execute a full OBSERVE→THINK→VERIFY cycle.

        This orchestrates the three-phase reasoning process:
        1. OBSERVE: Extract facts, assumptions, uncertainties from input
        2. THINK: Build a chain of reasoning steps to answer the query
        3. VERIFY: Check reasoning for consistency and safety

        Args:
            query: The user's query to reason about
            context: Available context (RAG results, conversation history, etc.)
            agent_spec: The agent specification (for model preferences and verification requirements)
            require_verification: Whether to enforce verification (always True for safety-critical agents)

        Returns:
            ReasoningResult with complete reasoning trace
        """
        log.info(
            "reasoning.starting",
            query_length=len(query),
            context_length=len(context),
            agent_id=agent_spec.agent_id,
            require_verification=require_verification,
        )

        # OBSERVE: gather key facts, assumptions, and uncertainties
        observation = await self._observe(query, context)
        log.debug(
            "reasoning.observe_complete",
            key_facts=len(observation.key_facts),
            assumptions=len(observation.assumptions),
            uncertainties=len(observation.uncertainties),
        )

        # THINK: generate reasoning steps from the observation
        reasoning_steps = await self._think(observation, query)
        log.debug(
            "reasoning.think_complete",
            steps=len(reasoning_steps),
        )

        # Calculate overall confidence from reasoning steps
        if reasoning_steps:
            total_confidence = sum(step.confidence for step in reasoning_steps) / len(reasoning_steps)
        else:
            total_confidence = 0.0

        # Extract final conclusion from last reasoning step
        conclusion = reasoning_steps[-1].conclusion if reasoning_steps else "No conclusion reached"

        # VERIFY: check reasoning for logical consistency
        verification = await self._verify(
            steps=reasoning_steps,
            observation=observation,
            agent_spec=agent_spec,
        )
        log.debug(
            "reasoning.verify_complete",
            is_verified=verification.is_verified,
            requires_review=verification.requires_human_review,
        )

        # Enforce verification requirement for safety-critical agents
        if require_verification and agent_spec.requires_verification:
            if not verification.is_verified or total_confidence < 0.6:
                verification.requires_human_review = True
                if not verification.review_reason:
                    verification.review_reason = "Safety-critical agent requires human review"

        result = SpecialistReasoningResult(
            observation=observation,
            reasoning_steps=reasoning_steps,
            conclusion=conclusion,
            verification=verification,
            total_confidence=total_confidence,
        )

        log.info(
            "reasoning.complete",
            agent_id=agent_spec.agent_id,
            confidence=f"{total_confidence:.2f}",
            verified=verification.is_verified,
            requires_review=verification.requires_human_review,
        )

        return result

    async def _observe(self, query: str, context: str) -> Observation:
        """OBSERVE: Extract key facts, assumptions, uncertainties.

        Uses an LLM call with structured prompting to identify:
        - Key facts: What is definitively stated in the query and context
        - Assumptions: What is being assumed but not explicitly stated
        - Uncertainties: What is unknown or ambiguous
        - Data sources: Where information came from (citations, references)

        Args:
            query: The user's query
            context: Available context (RAG, conversation history)

        Returns:
            Observation with extracted information
        """
        prompt = f"""Analyze the following query and context. Extract key information in JSON format.

Query: {query}

Context: {context[:2000]}

Provide your analysis in this exact JSON structure:
{{
    "key_facts": ["fact 1", "fact 2", ...],
    "assumptions": ["assumption 1", "assumption 2", ...],
    "uncertainties": ["uncertainty 1", "uncertainty 2", ...],
    "data_sources": ["source 1", "source 2", ...]
}}

Focus on:
- Key facts: Concrete information stated in query/context
- Assumptions: Implicit assumptions being made
- Uncertainties: What is unknown or needs clarification
- Data sources: Documents, procedures, or sources referenced

Respond ONLY with valid JSON, no additional text."""

        messages = [
            {"role": "system", "content": "You are a precise analytical assistant. Always respond with valid JSON only."},
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self._llm.complete(
                messages=messages,
                temperature=0.3,  # Low temperature for deterministic extraction
                max_tokens=1024,
            )
            response_text = self._llm.extract_text(response)

            # Try to parse JSON response
            parsed = json.loads(response_text)

            return Observation(
                key_facts=parsed.get("key_facts", []),
                assumptions=parsed.get("assumptions", []),
                uncertainties=parsed.get("uncertainties", []),
                data_sources=parsed.get("data_sources", []),
            )

        except json.JSONDecodeError:
            # Fallback: extract from text if JSON parsing fails
            log.warning("reasoning.observe_json_failed", response=response_text[:200])
            return Observation(
                key_facts=["Could not extract facts (JSON parse error)"],
                assumptions=[],
                uncertainties=["Unable to fully analyze query"],
                data_sources=[],
            )
        except Exception as exc:
            log.error("reasoning.observe_failed", error=str(exc))
            return Observation(
                key_facts=["Error during observation phase"],
                assumptions=[],
                uncertainties=["Analysis failed"],
                data_sources=[],
            )

    async def _think(self, observation: Observation, query: str) -> list[ReasoningStep]:
        """THINK: Build chain of reasoning steps.

        Uses an LLM call to construct a logical chain of reasoning steps that
        lead from the observed facts to a conclusion. Each step includes:
        - Description of the reasoning
        - Supporting evidence
        - Conclusion reached
        - Confidence level

        Args:
            observation: The observation from OBSERVE phase
            query: The original query

        Returns:
            List of reasoning steps
        """
        facts_text = "\n".join(f"- {fact}" for fact in observation.key_facts)
        assumptions_text = "\n".join(f"- {a}" for a in observation.assumptions)
        uncertainties_text = "\n".join(f"- {u}" for u in observation.uncertainties)

        prompt = f"""Given the following observations, build a logical reasoning chain to answer the query.

Query: {query}

Key Facts:
{facts_text or "- None identified"}

Assumptions:
{assumptions_text or "- None identified"}

Uncertainties:
{uncertainties_text or "- None identified"}

Build a step-by-step reasoning chain. For each step, provide:
- Description of the reasoning
- Evidence supporting this step
- Conclusion from this step
- Confidence (0.0 to 1.0)

Respond in this JSON format:
{{
    "steps": [
        {{
            "step_number": 1,
            "description": "First reasoning step...",
            "evidence": ["evidence 1", "evidence 2"],
            "conclusion": "Intermediate conclusion...",
            "confidence": 0.85
        }},
        ...
    ]
}}

Build 3-7 steps that logically connect the facts to a final conclusion.
Respond ONLY with valid JSON, no additional text."""

        messages = [
            {"role": "system", "content": "You are a logical reasoning assistant. Always respond with valid JSON only."},
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self._llm.complete(
                messages=messages,
                temperature=0.3,
                max_tokens=2048,
            )
            response_text = self._llm.extract_text(response)

            # Parse JSON response
            parsed = json.loads(response_text)
            steps_data = parsed.get("steps", [])

            reasoning_steps = [
                ReasoningStep(
                    step_number=step.get("step_number", idx + 1),
                    description=step.get("description", ""),
                    evidence=step.get("evidence", []),
                    conclusion=step.get("conclusion", ""),
                    confidence=float(step.get("confidence", 0.5)),
                )
                for idx, step in enumerate(steps_data)
            ]

            return reasoning_steps

        except json.JSONDecodeError:
            log.warning("reasoning.think_json_failed", response=response_text[:200])
            # Fallback: create a single low-confidence step
            return [
                ReasoningStep(
                    step_number=1,
                    description="Unable to build structured reasoning chain (JSON parse error)",
                    evidence=observation.key_facts[:3],
                    conclusion="Analysis incomplete",
                    confidence=0.3,
                )
            ]
        except Exception as exc:
            log.error("reasoning.think_failed", error=str(exc))
            return [
                ReasoningStep(
                    step_number=1,
                    description="Error during reasoning phase",
                    evidence=[],
                    conclusion="Reasoning failed",
                    confidence=0.0,
                )
            ]

    async def _verify(
        self,
        steps: list[ReasoningStep],
        observation: Observation,
        agent_spec: AgentSpec,
    ) -> Verification:
        """VERIFY: Check reasoning chain for consistency and safety.

        Uses an LLM call to verify:
        - Internal consistency: Do steps logically follow from each other?
        - Evidence backing: Are conclusions supported by evidence?
        - Safety flags: Any safety concerns for safety-critical agents?
        - Confidence threshold: Is overall confidence sufficient?

        Args:
            steps: The reasoning steps from THINK phase
            observation: The observation from OBSERVE phase
            agent_spec: Agent specification (for safety requirements)

        Returns:
            Verification result
        """
        steps_text = "\n\n".join(
            f"Step {step.step_number}: {step.description}\n"
            f"Evidence: {', '.join(step.evidence)}\n"
            f"Conclusion: {step.conclusion}\n"
            f"Confidence: {step.confidence:.2f}"
            for step in steps
        )

        is_safety_critical = agent_spec.requires_verification

        prompt = f"""Verify the following reasoning chain for consistency and correctness.

Safety-Critical Agent: {is_safety_critical}

Reasoning Chain:
{steps_text}

Original Key Facts:
{chr(10).join(f"- {fact}" for fact in observation.key_facts)}

Check for:
1. Internal consistency: Do steps logically follow?
2. Evidence backing: Are conclusions supported?
3. Safety concerns: Any flags for safety-critical decisions?
4. Confidence: Are confidence scores reasonable?

Respond in this JSON format:
{{
    "is_verified": true/false,
    "checks_passed": ["check 1", "check 2", ...],
    "checks_failed": ["failure 1", "failure 2", ...],
    "requires_human_review": true/false,
    "review_reason": "Reason if review needed, or null"
}}

Respond ONLY with valid JSON, no additional text."""

        messages = [
            {"role": "system", "content": "You are a verification assistant. Always respond with valid JSON only."},
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self._llm.complete(
                messages=messages,
                temperature=0.3,
                max_tokens=1024,
            )
            response_text = self._llm.extract_text(response)

            # Parse JSON response
            parsed = json.loads(response_text)

            return Verification(
                is_verified=parsed.get("is_verified", False),
                checks_passed=parsed.get("checks_passed", []),
                checks_failed=parsed.get("checks_failed", []),
                requires_human_review=parsed.get("requires_human_review", True),
                review_reason=parsed.get("review_reason"),
            )

        except json.JSONDecodeError:
            log.warning("reasoning.verify_json_failed", response=response_text[:200])
            # Fallback: conservative verification (fail-safe)
            return Verification(
                is_verified=False,
                checks_passed=[],
                checks_failed=["Unable to parse verification result"],
                requires_human_review=True,
                review_reason="Verification JSON parse error - defaulting to human review",
            )
        except Exception as exc:
            log.error("reasoning.verify_failed", error=str(exc))
            return Verification(
                is_verified=False,
                checks_passed=[],
                checks_failed=["Verification process failed"],
                requires_human_review=True,
                review_reason=f"Verification error: {str(exc)}",
            )
