"""Thinking tools for meta-cognitive agent operations.

This package provides advanced reasoning tools that agents can invoke for
complex decision-making scenarios:

- **RedTeam**: Adversarial analysis to stress-test responses
- **Council**: Multi-perspective deliberation for complex decisions
- **FirstPrinciples**: Recursive decomposition to fundamental truths

All tools follow a common pattern:
1. Accept context and reasoning state
2. Use LLM calls with structured JSON prompting
3. Return results with confidence scores and human review flags
4. Aggregate into ThinkingToolOutput for the orchestrator

Usage:
    from src.agent.thinking import ThinkingToolOutput, RedTeam, Council, FirstPrinciples

    # In reasoning engine or orchestrator
    red_team = RedTeam(llm_client)
    result = await red_team.analyze(response=draft, sources=sources, clearance="class_ii")

    # Aggregate results
    output = ThinkingToolOutput(
        red_team=result,
        council=None,
        first_principles=None,
    )

    if output.requires_human_review:
        # Block response, escalate to human
        ...
"""

from __future__ import annotations

from dataclasses import dataclass

from src.agent.thinking.council import Council, CouncilResult
from src.agent.thinking.first_principles import FirstPrinciples, FirstPrinciplesResult
from src.agent.thinking.red_team import RedTeam, RedTeamResult

__all__ = [
    "ThinkingToolOutput",
    "RedTeam",
    "RedTeamResult",
    "Council",
    "CouncilResult",
    "FirstPrinciples",
    "FirstPrinciplesResult",
]


@dataclass
class ThinkingToolOutput:
    """Aggregated output from all invoked thinking tools.

    This bundles results from thinking tools into a single structure that
    the orchestrator can inspect to determine if the response needs adjustment
    or human review.

    Attributes:
        red_team: Result from adversarial analysis (None if not invoked)
        council: Result from multi-perspective deliberation (None if not invoked)
        first_principles: Result from fundamental decomposition (None if not invoked)
    """

    red_team: RedTeamResult | None = None
    council: CouncilResult | None = None
    first_principles: FirstPrinciplesResult | None = None

    @property
    def any_invoked(self) -> bool:
        """Return True if any thinking tool was invoked."""
        return any([self.red_team, self.council, self.first_principles])

    @property
    def requires_human_review(self) -> bool:
        """Return True if any tool flagged for human review.

        This is the primary gate for escalation. If any thinking tool
        identified CRITICAL issues or low confidence, we block the response
        and require human oversight.
        """
        if self.red_team and self.red_team.requires_human_review:
            return True
        if self.council and self.council.requires_human_review:
            return True
        if self.first_principles and self.first_principles.requires_human_review:
            return True
        return False

    @property
    def adjusted_confidence(self) -> float:
        """Compute adjusted confidence from all invoked tools.

        Takes the minimum confidence across all tools (most conservative).
        Returns 1.0 if no tools were invoked.
        """
        confidences = []

        if self.red_team:
            confidences.append(self.red_team.overall_confidence)
        if self.council:
            confidences.append(self.council.consensus_confidence)
        if self.first_principles:
            confidences.append(self.first_principles.reconstruction_confidence)

        return min(confidences) if confidences else 1.0
