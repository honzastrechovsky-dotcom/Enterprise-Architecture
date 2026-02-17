"""Council-based multi-perspective deliberation.

This module implements a multi-agent debate system where different perspectives
critique each other to reach a more robust consensus. Useful when multiple valid
approaches exist or complex tradeoffs need evaluation.

The council uses a 3-round process:
1. Round 1: Generate 3 initial positions (parallel)
2. Round 2: Each position critiques the others (3 parallel critiques)
3. Round 3: Synthesize consensus with dissent notes

This results in 7 LLM calls total (3+3 parallel + 1 synthesis).

Usage:
    council = Council(llm_client)
    result = await council.deliberate(
        query="Should we migrate to Kubernetes?",
        context="Current infrastructure: ...",
    )

    # Use consensus as the recommendation
    print(result.consensus)

    # Note dissenting views
    for dissent in result.dissenting_views:
        print(f"Dissent: {dissent}")
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import structlog

from src.agent.llm import LLMClient

log = structlog.get_logger(__name__)

# Configuration
NUM_PERSPECTIVES = 3  # Number of initial positions to generate


@dataclass
class Perspective:
    """A single perspective in the council deliberation.

    Each perspective represents a viewpoint or approach to the problem,
    with supporting arguments and critiques of other perspectives.

    Attributes:
        name: Name of this perspective (e.g., "Security-First Approach")
        position: The core position or recommendation
        arguments: Supporting arguments for this position
        critiques: Critiques of other perspectives (populated in round 2)
        confidence: Confidence in this perspective (0.0-1.0)
    """

    name: str
    position: str
    arguments: list[str]
    critiques: list[str] = None
    confidence: float = 0.7


@dataclass
class CouncilResult:
    """Complete result from council deliberation.

    Attributes:
        perspectives: All perspectives generated in round 1
        consensus: The synthesized consensus position
        consensus_confidence: Confidence in the consensus (0.0-1.0)
        dissenting_views: Views that significantly diverge from consensus
        requires_human_review: True if perspectives deeply conflict
        review_reason: Explanation if human review is required
    """

    perspectives: list[Perspective]
    consensus: str
    consensus_confidence: float
    dissenting_views: list[str]
    requires_human_review: bool
    review_reason: str | None


class Council:
    """Multi-perspective deliberation engine.

    Generates multiple perspectives on a problem, has them critique each other,
    then synthesizes a consensus that acknowledges dissent.

    The 3-round process:
    - Round 1: Generate diverse perspectives (3 parallel LLM calls)
    - Round 2: Cross-critique (3 parallel LLM calls)
    - Round 3: Synthesize consensus (1 LLM call)

    Example:
        council = Council(llm_client)
        result = await council.deliberate(
            query="Should we adopt event sourcing?",
            context="Current: CRUD with PostgreSQL...",
        )

        if result.requires_human_review:
            log.warning("council.deep_conflict", reason=result.review_reason)

        print(result.consensus)
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize council deliberation engine.

        Args:
            llm_client: LLM client for deliberation calls
        """
        self._llm = llm_client

    async def deliberate(
        self,
        *,
        query: str,
        context: str,
    ) -> CouncilResult:
        """Execute multi-perspective deliberation on a query.

        This orchestrates the 3-round process:
        1. Generate initial perspectives (parallel)
        2. Generate critiques (parallel)
        3. Synthesize consensus

        Args:
            query: The query to deliberate on
            context: Available context

        Returns:
            CouncilResult with consensus and dissenting views
        """
        log.info(
            "council.starting",
            query_length=len(query),
            context_length=len(context),
            num_perspectives=NUM_PERSPECTIVES,
        )

        # Round 1: Generate initial positions (parallel)
        perspectives = await self._generate_positions(query, context)

        log.debug(
            "council.positions_generated",
            count=len(perspectives),
            names=[p.name for p in perspectives],
        )

        # Round 2: Generate critiques (parallel)
        perspectives = await self._generate_critiques(perspectives, query, context)

        log.debug("council.critiques_generated")

        # Round 3: Synthesize consensus
        synthesis = await self._synthesize(perspectives, query)

        result = CouncilResult(
            perspectives=perspectives,
            consensus=synthesis["consensus"],
            consensus_confidence=synthesis["confidence"],
            dissenting_views=synthesis["dissenting_views"],
            requires_human_review=synthesis["requires_review"],
            review_reason=synthesis.get("review_reason"),
        )

        log.info(
            "council.complete",
            consensus_length=len(result.consensus),
            confidence=f"{result.consensus_confidence:.2f}",
            dissenting_count=len(result.dissenting_views),
            requires_review=result.requires_human_review,
        )

        return result

    async def _generate_positions(
        self, query: str, context: str
    ) -> list[Perspective]:
        """Round 1: Generate diverse initial positions (parallel).

        Uses parallel LLM calls to generate multiple perspectives on the problem.
        Each perspective approaches the problem from a different angle.

        Args:
            query: The query to deliberate on
            context: Available context

        Returns:
            List of initial perspectives
        """
        # Define perspective prompts (diverse angles)
        perspective_prompts = [
            {
                "name": "Pragmatic Approach",
                "instructions": "Focus on practical implementation, quick wins, and minimal disruption. Consider cost, time, and team capacity.",
            },
            {
                "name": "Quality-First Approach",
                "instructions": "Prioritize long-term quality, maintainability, and correctness. Consider technical debt and future scalability.",
            },
            {
                "name": "Risk-Aware Approach",
                "instructions": "Focus on risks, failure modes, and safety. Consider what could go wrong and how to mitigate.",
            },
        ]

        async def generate_one_perspective(spec: dict) -> Perspective:
            """Generate a single perspective."""
            prompt = f"""You are participating in a council deliberation. Take the following perspective:

Perspective: {spec['name']}
Instructions: {spec['instructions']}

Query: {query}

Context: {context[:2000]}

From this perspective, provide your position in JSON format:
{{
    "position": "Your position/recommendation from this perspective",
    "arguments": ["argument 1", "argument 2", "argument 3", ...],
    "confidence": 0.0-1.0
}}

Provide 3-5 strong arguments supporting your perspective.
Respond ONLY with valid JSON, no additional text."""

            messages = [
                {
                    "role": "system",
                    "content": f"You are a council member representing the {spec['name']}. Always respond with valid JSON only.",
                },
                {"role": "user", "content": prompt},
            ]

            try:
                response = await self._llm.complete(
                    messages=messages,
                    temperature=0.6,  # Higher temperature for diverse perspectives
                    max_tokens=1024,
                )
                response_text = self._llm.extract_text(response)

                parsed = json.loads(response_text)

                return Perspective(
                    name=spec["name"],
                    position=parsed.get("position", "No position provided"),
                    arguments=parsed.get("arguments", []),
                    confidence=float(parsed.get("confidence", 0.7)),
                )

            except json.JSONDecodeError:
                log.warning(
                    "council.position_json_failed", perspective=spec["name"]
                )
                return Perspective(
                    name=spec["name"],
                    position="Unable to generate position (JSON parse error)",
                    arguments=[],
                    confidence=0.3,
                )
            except Exception as exc:
                log.error(
                    "council.position_failed",
                    perspective=spec["name"],
                    error=str(exc),
                )
                return Perspective(
                    name=spec["name"],
                    position=f"Error generating position: {str(exc)[:100]}",
                    arguments=[],
                    confidence=0.0,
                )

        # Generate all perspectives in parallel
        perspectives = await asyncio.gather(
            *[generate_one_perspective(spec) for spec in perspective_prompts]
        )

        return list(perspectives)

    async def _generate_critiques(
        self, perspectives: list[Perspective], query: str, context: str
    ) -> list[Perspective]:
        """Round 2: Generate critiques of other positions (parallel).

        Each perspective critiques the other perspectives, identifying
        weaknesses and counterarguments.

        Args:
            perspectives: Perspectives from round 1
            query: Original query
            context: Available context

        Returns:
            Perspectives with critiques populated
        """

        async def generate_critiques_for_perspective(
            perspective: Perspective, others: list[Perspective]
        ) -> list[str]:
            """Generate critiques from one perspective of the others."""
            others_text = "\n\n".join(
                f"Perspective: {other.name}\n"
                f"Position: {other.position}\n"
                f"Arguments: {', '.join(other.arguments)}"
                for other in others
            )

            prompt = f"""You are the {perspective.name} in a council deliberation.
Your position is: {perspective.position}

Critique the other perspectives:

{others_text}

Provide your critiques in JSON format:
{{
    "critiques": [
        "Critique of perspective 1...",
        "Critique of perspective 2...",
        ...
    ]
}}

Be constructive but identify genuine weaknesses or blind spots.
Respond ONLY with valid JSON, no additional text."""

            messages = [
                {
                    "role": "system",
                    "content": f"You are a council member representing the {perspective.name}. Always respond with valid JSON only.",
                },
                {"role": "user", "content": prompt},
            ]

            try:
                response = await self._llm.complete(
                    messages=messages,
                    temperature=0.5,
                    max_tokens=1024,
                )
                response_text = self._llm.extract_text(response)

                parsed = json.loads(response_text)
                return parsed.get("critiques", [])

            except json.JSONDecodeError:
                log.warning(
                    "council.critique_json_failed",
                    perspective=perspective.name,
                )
                return ["Unable to generate critiques (JSON parse error)"]
            except Exception as exc:
                log.error(
                    "council.critique_failed",
                    perspective=perspective.name,
                    error=str(exc),
                )
                return [f"Error generating critiques: {str(exc)[:100]}"]

        # Generate critiques for each perspective in parallel
        critique_tasks = []
        for perspective in perspectives:
            others = [p for p in perspectives if p.name != perspective.name]
            critique_tasks.append(
                generate_critiques_for_perspective(perspective, others)
            )

        all_critiques = await asyncio.gather(*critique_tasks)

        # Attach critiques to perspectives
        for perspective, critiques in zip(perspectives, all_critiques):
            perspective.critiques = critiques

        return perspectives

    async def _synthesize(
        self, perspectives: list[Perspective], query: str
    ) -> dict[str, any]:
        """Round 3: Synthesize consensus from all perspectives.

        Takes all perspectives and critiques to build a consensus position
        that acknowledges dissenting views.

        Args:
            perspectives: All perspectives with critiques
            query: Original query

        Returns:
            Dictionary with consensus, confidence, dissenting_views, requires_review
        """
        perspectives_text = "\n\n".join(
            f"Perspective: {p.name}\n"
            f"Position: {p.position}\n"
            f"Arguments: {', '.join(p.arguments)}\n"
            f"Critiques: {', '.join(p.critiques or [])}\n"
            f"Confidence: {p.confidence:.2f}"
            for p in perspectives
        )

        prompt = f"""You are synthesizing a council deliberation. Multiple perspectives
have been presented and critiqued. Build a consensus recommendation.

Original query: {query}

Perspectives and critiques:
{perspectives_text}

Provide synthesis in JSON format:
{{
    "consensus": "Synthesized consensus recommendation...",
    "confidence": 0.0-1.0,
    "dissenting_views": ["View 1 that doesn't align", "View 2 that doesn't align", ...],
    "requires_review": true/false,
    "review_reason": "Reason if review needed, or null"
}}

Guidelines:
- Find common ground across perspectives
- Acknowledge where perspectives conflict
- Include dissenting views that significantly diverge
- Flag for review if deep, unresolvable conflicts exist

Respond ONLY with valid JSON, no additional text."""

        messages = [
            {
                "role": "system",
                "content": "You are a synthesis moderator. Always respond with valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self._llm.complete(
                messages=messages,
                temperature=0.4,
                max_tokens=2048,
            )
            response_text = self._llm.extract_text(response)

            parsed = json.loads(response_text)

            return {
                "consensus": parsed.get("consensus", "Unable to reach consensus"),
                "confidence": float(parsed.get("confidence", 0.5)),
                "dissenting_views": parsed.get("dissenting_views", []),
                "requires_review": parsed.get("requires_review", False),
                "review_reason": parsed.get("review_reason"),
            }

        except json.JSONDecodeError:
            log.warning("council.synthesis_json_failed")
            # Fallback: conservative synthesis
            return {
                "consensus": "Unable to synthesize consensus (JSON parse error). All perspectives should be reviewed.",
                "confidence": 0.3,
                "dissenting_views": [p.position for p in perspectives],
                "requires_review": True,
                "review_reason": "Synthesis failed, unable to parse result",
            }
        except Exception as exc:
            log.error("council.synthesis_failed", error=str(exc))
            return {
                "consensus": f"Synthesis error: {str(exc)[:100]}",
                "confidence": 0.0,
                "dissenting_views": [p.position for p in perspectives],
                "requires_review": True,
                "review_reason": f"Synthesis failed: {str(exc)}",
            }
