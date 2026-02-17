"""AI usage disclosure.

All AI-generated content must be clearly identified. This module provides
standard disclosure formatting for responses to ensure users understand
they are interacting with AI and should verify critical outputs.
"""

from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)


class DisclosureService:
    """Adds AI usage disclosures to responses.

    Usage:
        disclosure = DisclosureService()
        response_with_disclosure = disclosure.add_disclosure(
            response=ai_response,
            model_used="gpt-4o",
            agent_id="agent-123",
        )
    """

    def add_disclosure(
        self,
        response: str,
        model_used: str,
        agent_id: str | None = None,
    ) -> str:
        """Append standard AI disclosure footer to a response.

        The disclosure clearly identifies AI-generated content and
        reminds users that verification is required.

        Args:
            response: The AI-generated response text
            model_used: Identifier of the LLM used (e.g., "gpt-4o", "claude-3-opus")
            agent_id: Optional agent identifier for traceability

        Returns:
            Response with disclosure footer appended
        """
        agent_info = f" | Agent: {agent_id}" if agent_id else ""

        disclosure_footer = (
            f"\n\n---\n"
            f"⚠️ AI-Generated Content | Model: {model_used}{agent_info} | Verification Required"
        )

        log.debug(
            "disclosure.added",
            model=model_used,
            agent_id=agent_id,
            response_length=len(response),
        )

        return response + disclosure_footer

    def format_verification_notice(self, requires_verification: bool) -> str:
        """Format a verification notice based on requirements.

        Args:
            requires_verification: Whether the content requires human verification

        Returns:
            Formatted verification notice text
        """
        if requires_verification:
            return (
                "⚠️ This AI-generated content requires human verification before use in "
                "production, safety-critical, or regulatory contexts."
            )

        return (
            "ℹ️ This content was generated with AI assistance. "
            "Review recommended for accuracy."
        )
