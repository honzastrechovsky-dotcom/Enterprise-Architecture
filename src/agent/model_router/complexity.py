"""Task complexity estimation for intelligent model routing.

The ComplexityEstimator analyzes multiple factors to produce a complexity
score (0.0-1.0) that drives model tier selection:

Factors analyzed:
- Message length and vocabulary richness
- Number of entities/topics mentioned
- Context window size required
- Agent capability requirements (safety-critical = heavier)
- Conversation depth
- Whether thinking tools are needed

Score → tier mapping:
- 0.0-0.3: LIGHT tier (7B models)
- 0.3-0.7: STANDARD tier (32B models)
- 0.7-1.0: HEAVY tier (72B models)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)


@dataclass
class TaskComplexity:
    """Result of task complexity estimation.

    Attributes:
        score: Overall complexity score (0.0-1.0)
        factors: Dict of individual factor scores for observability
        recommended_tier: Recommended model tier based on score
    """

    score: float
    factors: dict[str, float]
    recommended_tier: str

    def __post_init__(self) -> None:
        """Validate complexity score."""
        if not 0.0 <= self.score <= 1.0:
            raise ValueError(f"Complexity score must be 0.0-1.0, got {self.score}")


class ComplexityEstimator:
    """Estimates task complexity using multiple heuristic factors.

    Uses multiple heuristic factors. For data-driven improvements,
    ML-based complexity prediction trained on historical routing data may be added.
    """

    # Thresholds for tier recommendations
    LIGHT_THRESHOLD = 0.3
    STANDARD_THRESHOLD = 0.7

    # Keywords that indicate high complexity
    COMPLEX_KEYWORDS = {
        "analyze",
        "architecture",
        "design",
        "security",
        "vulnerability",
        "threat",
        "reasoning",
        "explain",
        "why",
        "compare",
        "evaluate",
        "assess",
        "review",
        "audit",
        "compliance",
    }

    # Safety-critical capabilities that warrant heavier models
    CRITICAL_CAPABILITIES = {
        "security_analysis",
        "compliance_review",
        "audit",
        "export_control",
        "classification",
    }

    def __init__(self) -> None:
        """Initialize complexity estimator."""
        log.debug("complexity_estimator.initialized")

    async def estimate(
        self,
        message: str,
        context_length: int = 0,
        agent_capabilities: list[str] | None = None,
        history_length: int = 0,
    ) -> TaskComplexity:
        """Estimate complexity of a task from multiple factors.

        Args:
            message: User message to analyze
            context_length: Number of tokens in RAG context
            agent_capabilities: List of agent capability tags
            history_length: Number of prior conversation turns

        Returns:
            TaskComplexity with score, factor breakdown, and recommendation
        """
        factors: dict[str, float] = {}

        # Factor 1: Message complexity (0.0-1.0)
        factors["message_complexity"] = self._analyze_message(message)

        # Factor 2: Context window requirement (0.0-1.0)
        factors["context_requirement"] = self._analyze_context(context_length)

        # Factor 3: Agent capability criticality (0.0-1.0)
        factors["capability_criticality"] = self._analyze_capabilities(
            agent_capabilities or []
        )

        # Factor 4: Conversation depth (0.0-1.0)
        factors["conversation_depth"] = self._analyze_conversation_depth(history_length)

        # Factor 5: Keyword complexity signals (0.0-1.0)
        factors["keyword_signals"] = self._analyze_keywords(message)

        # Weighted average of factors
        # Message and keywords are most important, context and depth are secondary
        weights = {
            "message_complexity": 0.3,
            "context_requirement": 0.15,
            "capability_criticality": 0.25,
            "conversation_depth": 0.1,
            "keyword_signals": 0.2,
        }

        score = sum(factors[key] * weights[key] for key in factors)
        score = max(0.0, min(1.0, score))  # Clamp to [0.0, 1.0]

        # Determine recommended tier
        if score < self.LIGHT_THRESHOLD:
            recommended_tier = "light"
        elif score < self.STANDARD_THRESHOLD:
            recommended_tier = "standard"
        else:
            recommended_tier = "heavy"

        log.info(
            "complexity_estimator.estimated",
            score=score,
            recommended_tier=recommended_tier,
            factors=factors,
        )

        return TaskComplexity(
            score=score,
            factors=factors,
            recommended_tier=recommended_tier,
        )

    def _analyze_message(self, message: str) -> float:
        """Analyze message length and vocabulary complexity.

        Factors:
        - Word count (longer = more complex)
        - Unique word ratio (higher = more complex)
        - Sentence count (more = more complex)
        - Average word length (longer = more complex)

        Returns:
            Complexity factor 0.0-1.0
        """
        if not message:
            return 0.0

        words = message.split()
        word_count = len(words)

        if word_count == 0:
            return 0.0

        # Word count scoring (0-50 words = 0.0, 200+ words = 1.0)
        word_score = min(word_count / 200.0, 1.0)

        # Unique word ratio (low ratio = repetitive/simple)
        unique_words = len(set(word.lower() for word in words))
        uniqueness_score = unique_words / word_count

        # Sentence count (more sentences = more ideas)
        sentence_count = len(re.split(r'[.!?]+', message))
        sentence_score = min(sentence_count / 10.0, 1.0)

        # Average word length
        avg_word_length = sum(len(word) for word in words) / word_count
        length_score = min((avg_word_length - 3.0) / 5.0, 1.0)  # 3-8 chars normalized

        # Weighted combination
        complexity = (
            word_score * 0.3
            + uniqueness_score * 0.3
            + sentence_score * 0.2
            + length_score * 0.2
        )

        return max(0.0, min(complexity, 1.0))

    def _analyze_context(self, context_length: int) -> float:
        """Analyze context window requirement.

        Larger context = more information to process = higher complexity.

        Args:
            context_length: Number of tokens in RAG context

        Returns:
            Complexity factor 0.0-1.0
        """
        # 0 tokens = 0.0, 2048+ tokens = 1.0
        if context_length <= 0:
            return 0.0

        score = min(context_length / 2048.0, 1.0)
        return score

    def _analyze_capabilities(self, capabilities: list[str]) -> float:
        """Analyze whether agent capabilities indicate critical/complex task.

        Safety-critical capabilities (security, compliance, audit) warrant
        heavier models for higher accuracy.

        Args:
            capabilities: List of agent capability tags

        Returns:
            Complexity factor 0.0-1.0
        """
        if not capabilities:
            return 0.0

        # Check for critical capabilities
        critical_count = sum(
            1 for cap in capabilities if cap in self.CRITICAL_CAPABILITIES
        )

        if critical_count > 0:
            # Any critical capability pushes toward heavier models
            return min(0.5 + (critical_count * 0.2), 1.0)

        return 0.0

    def _analyze_conversation_depth(self, history_length: int) -> float:
        """Analyze conversation depth.

        Longer conversations may have more context dependencies and require
        better reasoning.

        Args:
            history_length: Number of prior conversation turns

        Returns:
            Complexity factor 0.0-1.0
        """
        # 0 turns = 0.0, 20+ turns = 1.0
        if history_length <= 0:
            return 0.0

        score = min(history_length / 20.0, 1.0)
        return score

    def _analyze_keywords(self, message: str) -> float:
        """Analyze keywords that signal complexity.

        Certain keywords indicate analytical/reasoning tasks that benefit
        from heavier models.

        Args:
            message: User message to analyze

        Returns:
            Complexity factor 0.0-1.0
        """
        if not message:
            return 0.0

        message_lower = message.lower()
        words = set(re.findall(r'\b\w+\b', message_lower))

        # Count complex keywords
        complex_count = sum(1 for keyword in self.COMPLEX_KEYWORDS if keyword in words)

        if complex_count == 0:
            return 0.0

        # 1 keyword = 0.3, 3+ keywords = 1.0
        score = min(0.3 + (complex_count * 0.2), 1.0)
        return score
