"""First principles decomposition for fundamental reasoning.

This module implements recursive decomposition to fundamental truths, following
the first principles thinking methodology:

1. Decompose: Break down the problem recursively by asking "why?" at each level
2. Challenge: Question assumptions at each node in the tree
3. Reconstruct: Build up from fundamental truths to answer the original query

The decomposition uses a tree structure with configurable depth and branching:
- MAX_DEPTH: How many levels of "why?" to ask (default 4)
- MAX_BRANCHES: How many sub-questions per node (default 3)

This results in 3-7 LLM calls depending on the recursion depth and branching factor.

Usage:
    fp = FirstPrinciples(llm_client)
    result = await fp.decompose(
        query="Should we use microservices for this system?",
        context="Current system is monolithic...",
    )

    # Use reconstruction as the answer
    print(result.reconstruction)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import structlog

from src.agent.llm import LLMClient

log = structlog.get_logger(__name__)

# Configuration for recursion depth and branching
MAX_DEPTH = 4  # Maximum recursion depth for "why?" questions
MAX_BRANCHES = 3  # Maximum sub-questions per node


@dataclass
class PrincipleNode:
    """A node in the first principles decomposition tree.

    Each node represents a question/answer pair at a particular depth in the
    reasoning tree. Leaf nodes are fundamental truths that can't be decomposed further.

    Attributes:
        question: The question being asked at this node
        answer: The answer or fundamental truth
        depth: How deep in the tree (0 = root)
        is_fundamental: True if this can't be decomposed further
        children: Sub-questions that arise from this node
        assumptions: Assumptions challenged at this node
    """

    question: str
    answer: str
    depth: int
    is_fundamental: bool = False
    children: list[PrincipleNode] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


@dataclass
class FirstPrinciplesResult:
    """Complete result from first principles decomposition.

    Attributes:
        root: Root node of the decomposition tree
        fundamental_truths: All leaf nodes (fundamental principles)
        reconstruction: Bottom-up answer built from fundamentals
        reconstruction_confidence: Confidence in the reconstructed answer (0.0-1.0)
        requires_human_review: True if fundamental assumptions are questionable
        review_reason: Explanation if human review is required
    """

    root: PrincipleNode
    fundamental_truths: list[PrincipleNode]
    reconstruction: str
    reconstruction_confidence: float
    requires_human_review: bool
    review_reason: str | None


class FirstPrinciples:
    """First principles decomposition engine.

    Uses recursive "why?" questioning to break down complex queries into
    fundamental truths, then rebuilds the answer from the bottom up.

    The process:
    1. Start with the original query
    2. Recursively ask "why?" to decompose into sub-questions
    3. Stop when reaching fundamental truths (MAX_DEPTH or is_fundamental)
    4. Collect all leaf nodes as fundamental principles
    5. Synthesize answer from fundamentals (bottom-up)

    Example:
        fp = FirstPrinciples(llm_client)
        result = await fp.decompose(
            query="Why should we use Kubernetes?",
            context="We have 50 microservices...",
        )

        # Tree structure
        for truth in result.fundamental_truths:
            print(f"Depth {truth.depth}: {truth.answer}")

        # Reconstructed answer from fundamentals
        print(result.reconstruction)
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize first principles engine.

        Args:
            llm_client: LLM client for decomposition calls
        """
        self._llm = llm_client

    async def decompose(
        self,
        *,
        query: str,
        context: str,
    ) -> FirstPrinciplesResult:
        """Execute first principles decomposition on a query.

        This orchestrates the full decomposition process:
        1. Recursive decomposition to build the tree
        2. Collect fundamental truths (leaf nodes)
        3. Synthesize answer from fundamentals (bottom-up)

        Args:
            query: The query to decompose
            context: Available context

        Returns:
            FirstPrinciplesResult with tree and reconstruction
        """
        log.info(
            "first_principles.starting",
            query_length=len(query),
            context_length=len(context),
            max_depth=MAX_DEPTH,
        )

        # Build decomposition tree recursively
        root = await self._decompose_recursive(
            question=query, context=context, depth=0
        )

        # Collect all leaf nodes (fundamental truths)
        fundamental_truths = self._collect_leaf_nodes(root)

        log.debug(
            "first_principles.decomposed",
            fundamental_count=len(fundamental_truths),
            max_depth_reached=max(t.depth for t in fundamental_truths)
            if fundamental_truths
            else 0,
        )

        # Synthesize answer from fundamentals (bottom-up)
        synthesis_result = await self._synthesize_from_fundamentals(
            fundamentals=fundamental_truths, original_query=query
        )

        result = FirstPrinciplesResult(
            root=root,
            fundamental_truths=fundamental_truths,
            reconstruction=synthesis_result["reconstruction"],
            reconstruction_confidence=synthesis_result["confidence"],
            requires_human_review=synthesis_result["requires_review"],
            review_reason=synthesis_result.get("review_reason"),
        )

        log.info(
            "first_principles.complete",
            fundamental_count=len(fundamental_truths),
            confidence=f"{result.reconstruction_confidence:.2f}",
            requires_review=result.requires_human_review,
        )

        return result

    async def _decompose_recursive(
        self, question: str, context: str, depth: int
    ) -> PrincipleNode:
        """Recursively decompose a question into sub-questions.

        This builds the decomposition tree by asking "why?" at each level.
        Stops when reaching MAX_DEPTH or when the LLM determines the answer
        is fundamental (can't be decomposed further).

        Args:
            question: Question to decompose
            context: Available context
            depth: Current recursion depth

        Returns:
            PrincipleNode with children populated recursively
        """
        # Base case: reached max depth
        if depth >= MAX_DEPTH:
            log.debug("first_principles.max_depth_reached", depth=depth)
            answer = await self._get_fundamental_answer(question, context)
            return PrincipleNode(
                question=question,
                answer=answer,
                depth=depth,
                is_fundamental=True,
                children=[],
                assumptions=[],
            )

        # Ask LLM to decompose this question
        prompt = f"""You are applying first principles thinking. Decompose the following
question into fundamental sub-questions by asking "why?" and "what assumptions are we making?"

Question: {question}

Context: {context[:1500]}

Depth: {depth}/{MAX_DEPTH}

Provide your decomposition in JSON format:
{{
    "answer": "Brief answer to this question",
    "is_fundamental": true/false,
    "assumptions": ["assumption 1", "assumption 2", ...],
    "sub_questions": ["why sub-question 1?", "why sub-question 2?", ...]
}}

Guidelines:
- If this is a fundamental truth that can't be decomposed further, set is_fundamental=true
- Include up to {MAX_BRANCHES} sub-questions that probe deeper
- Identify key assumptions being made
- Sub-questions should ask "why?" or challenge assumptions

Respond ONLY with valid JSON, no additional text."""

        messages = [
            {
                "role": "system",
                "content": "You are a first principles thinking assistant. Always respond with valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self._llm.complete(
                messages=messages,
                temperature=0.4,  # Moderate temperature for creative decomposition
                max_tokens=1024,
            )
            response_text = self._llm.extract_text(response)

            parsed = json.loads(response_text)

            answer = parsed.get("answer", "No answer provided")
            is_fundamental = parsed.get("is_fundamental", False)
            assumptions = parsed.get("assumptions", [])
            sub_questions = parsed.get("sub_questions", [])

            # Create current node
            node = PrincipleNode(
                question=question,
                answer=answer,
                depth=depth,
                is_fundamental=is_fundamental,
                children=[],
                assumptions=assumptions,
            )

            # If fundamental or no sub-questions, stop recursion
            if is_fundamental or not sub_questions:
                return node

            # Recursively decompose sub-questions (limit to MAX_BRANCHES)
            for sub_q in sub_questions[:MAX_BRANCHES]:
                child_node = await self._decompose_recursive(
                    question=sub_q, context=context, depth=depth + 1
                )
                node.children.append(child_node)

            return node

        except json.JSONDecodeError:
            log.warning(
                "first_principles.decompose_json_failed",
                depth=depth,
                response=response_text[:200],
            )
            # Fallback: treat as fundamental
            return PrincipleNode(
                question=question,
                answer="Unable to decompose (JSON parse error)",
                depth=depth,
                is_fundamental=True,
                children=[],
                assumptions=["Decomposition failed"],
            )
        except Exception as exc:
            log.error("first_principles.decompose_failed", depth=depth, error=str(exc))
            # Fallback: treat as fundamental
            return PrincipleNode(
                question=question,
                answer=f"Error during decomposition: {str(exc)[:100]}",
                depth=depth,
                is_fundamental=True,
                children=[],
                assumptions=["Error occurred"],
            )

    async def _get_fundamental_answer(self, question: str, context: str) -> str:
        """Get a fundamental answer when max depth is reached.

        Args:
            question: The question to answer
            context: Available context

        Returns:
            A concise fundamental answer
        """
        prompt = f"""Provide a fundamental, foundational answer to this question.
This should be a truth that doesn't require further decomposition.

Question: {question}

Context: {context[:1500]}

Provide a concise fundamental answer (1-2 sentences)."""

        messages = [
            {
                "role": "system",
                "content": "You are a first principles assistant. Provide fundamental truths.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            response = await self._llm.complete(
                messages=messages,
                temperature=0.3,
                max_tokens=256,
            )
            return self._llm.extract_text(response).strip()

        except Exception as exc:
            log.error("first_principles.fundamental_answer_failed", error=str(exc))
            return "Unable to determine fundamental answer"

    def _collect_leaf_nodes(self, root: PrincipleNode) -> list[PrincipleNode]:
        """Collect all leaf nodes (fundamental truths) from the tree.

        Args:
            root: Root node of the decomposition tree

        Returns:
            List of all leaf nodes (nodes with no children)
        """
        leaves = []

        def traverse(node: PrincipleNode) -> None:
            if not node.children:
                leaves.append(node)
            else:
                for child in node.children:
                    traverse(child)

        traverse(root)
        return leaves

    async def _synthesize_from_fundamentals(
        self, fundamentals: list[PrincipleNode], original_query: str
    ) -> dict[str, any]:
        """Synthesize answer from fundamental truths (bottom-up).

        Takes all leaf nodes (fundamental truths) and builds a coherent answer
        to the original query by reasoning from first principles.

        Args:
            fundamentals: All fundamental truths (leaf nodes)
            original_query: The original query being answered

        Returns:
            Dictionary with reconstruction, confidence, requires_review, review_reason
        """
        if not fundamentals:
            return {
                "reconstruction": "No fundamental principles identified",
                "confidence": 0.0,
                "requires_review": True,
                "review_reason": "Decomposition produced no fundamental truths",
            }

        fundamentals_text = "\n".join(
            f"- [Depth {f.depth}] {f.question}\n  Answer: {f.answer}"
            for f in fundamentals
        )

        assumptions_text = "\n".join(
            f"- {a}"
            for node in fundamentals
            for a in node.assumptions
            if node.assumptions
        )

        prompt = f"""You have decomposed a query into fundamental principles. Now synthesize
a coherent answer from the bottom up, starting from these fundamentals.

Original query: {original_query}

Fundamental principles discovered:
{fundamentals_text}

Key assumptions challenged:
{assumptions_text or "None"}

Build your answer from first principles. Start with the fundamental truths and
reason upward to answer the original query.

Respond in JSON format:
{{
    "reconstruction": "Your answer built from first principles...",
    "confidence": 0.0-1.0,
    "requires_review": true/false,
    "review_reason": "Reason if review needed, or null"
}}

Guidelines:
- Base reasoning on fundamental truths, not assumptions
- Acknowledge where assumptions were challenged
- Flag for review if fundamentals reveal questionable assumptions

Respond ONLY with valid JSON, no additional text."""

        messages = [
            {
                "role": "system",
                "content": "You are a synthesis assistant. Always respond with valid JSON only.",
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
                "reconstruction": parsed.get(
                    "reconstruction", "Unable to synthesize answer"
                ),
                "confidence": float(parsed.get("confidence", 0.5)),
                "requires_review": parsed.get("requires_review", False),
                "review_reason": parsed.get("review_reason"),
            }

        except json.JSONDecodeError:
            log.warning("first_principles.synthesis_json_failed")
            return {
                "reconstruction": "Unable to synthesize from fundamentals (JSON parse error)",
                "confidence": 0.3,
                "requires_review": True,
                "review_reason": "Synthesis failed, unable to parse result",
            }
        except Exception as exc:
            log.error("first_principles.synthesis_failed", error=str(exc))
            return {
                "reconstruction": f"Synthesis error: {str(exc)[:100]}",
                "confidence": 0.0,
                "requires_review": True,
                "review_reason": f"Synthesis failed: {str(exc)}",
            }
