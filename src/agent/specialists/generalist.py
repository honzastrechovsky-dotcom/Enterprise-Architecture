"""Generalist Agent - the default general-purpose assistant.

This agent handles general Q&A, conversation, and queries that don't require
specialist expertise. It wraps the existing runtime behavior into the new
specialist agent system.
"""

from __future__ import annotations

import structlog

from src.agent.registry import AgentSpec
from src.agent.specialists.base import (
    AgentContext,
    AgentResponse,
    BaseSpecialistAgent,
)
from src.models.user import UserRole

log = structlog.get_logger(__name__)

# Use the existing system prompt from runtime.py
_SYSTEM_PROMPT = """You are an enterprise AI assistant. You have access to the organization's
document library. When answering questions, cite your sources using the provided context.

Guidelines:
- Be precise and factual. Cite documents when you use information from them.
- If you don't know the answer, say so clearly.
- Keep responses professional and appropriate for a business environment.
- For calculations, you can use the calculator tool.
- Never fabricate information or sources."""


class GeneralistAgent(BaseSpecialistAgent):
    """General-purpose conversational agent.

    This is the default agent that handles general Q&A, casual conversation,
    and queries that don't require specialist domain expertise. It provides
    broad coverage using document search and basic reasoning.
    """

    async def process(self, message: str, context: AgentContext) -> AgentResponse:
        """Process a general query or conversation turn.

        This agent:
        1. Uses RAG context if available
        2. Maintains conversation history
        3. Can use document search and calculator tools
        4. Provides straightforward, factual responses

        Args:
            message: User's message or question
            context: Full context including RAG and history

        Returns:
            AgentResponse with the assistant's reply
        """
        reasoning_trace = [
            "Generalist Agent activated for general query",
            f"Query: {message[:100]}..." if len(message) > 100 else f"Query: {message}",
        ]

        tools_used = []
        citations = []

        # Step 1: Check if we have sufficient RAG context
        # If not, and this seems like a factual query, search documents
        needs_search = self._should_search(message, context)

        if needs_search:
            reasoning_trace.append("Searching for relevant information")

            search_result = await self._use_tool(
                "document_search",
                {"query": message, "top_k": 5},
                context,
            )

            tools_used.append({
                "tool": "document_search",
                "success": search_result.success,
            })

            if search_result.success:
                reasoning_trace.append("Found relevant information in documents")
            else:
                reasoning_trace.append(
                    f"Search failed: {search_result.error}, proceeding with available context"
                )

        # Step 2: Build messages using standard format
        messages = self._build_messages(message, context)
        reasoning_trace.append(f"Built message context with {len(messages)} messages")

        # Step 3: Call LLM
        log.info(
            "generalist.processing",
            tenant_id=str(context.tenant_id),
            has_rag=bool(context.rag_context),
            conversation_turns=len(context.conversation_history),
        )

        response_text = await self._call_llm(messages)
        reasoning_trace.append(f"Generated response ({len(response_text)} chars)")

        # Step 4: Extract citations if RAG context was used
        if context.rag_context:
            citations.append({
                "source": "document_search",
                "note": "Information from organizational documents",
            })
            reasoning_trace.append("Added document citations")

        # Step 5: Generalist responses don't require verification unless
        # they involve sensitive operations (handled by orchestrator)
        verification_status = "passed"

        log.info(
            "generalist.complete",
            tenant_id=str(context.tenant_id),
            response_length=len(response_text),
            citations=len(citations),
        )

        return AgentResponse(
            content=response_text,
            agent_id=self.spec.agent_id,
            citations=citations,
            tools_used=tools_used,
            reasoning_trace=reasoning_trace,
            verification_status=verification_status,
            metadata={
                "conversation_turns": len(context.conversation_history),
                "rag_used": bool(context.rag_context),
            },
        )

    def _should_search(self, message: str, context: AgentContext) -> bool:
        """Determine if we should search for documents.

        Search if:
        - No RAG context provided (orchestrator didn't retrieve)
        - Message looks like a factual question (contains question words)
        - RAG context is too short to be useful

        Args:
            message: The user's message
            context: Full context

        Returns:
            True if we should search, False otherwise
        """
        # Don't search if we already have good RAG context
        if context.rag_context and len(context.rag_context) > 200:
            return False

        # Search if message looks like a question
        question_words = ["what", "why", "how", "when", "where", "who", "which", "is", "are", "can"]
        message_lower = message.lower()

        # Check if message starts with a question word or ends with ?
        looks_like_question = (
            message.endswith("?") or
            any(message_lower.startswith(word) for word in question_words)
        )

        # Also search if message contains "find", "search", "show me", etc.
        search_keywords = ["find", "search", "show me", "tell me about", "lookup", "look up"]
        contains_search_intent = any(keyword in message_lower for keyword in search_keywords)

        return looks_like_question or contains_search_intent


# Agent specification
SPEC = AgentSpec(
    agent_id="generalist",
    name="Generalist Assistant",
    description=(
        "General-purpose enterprise AI assistant for Q&A and conversation. "
        "Handles queries that don't require specialist expertise. Uses document "
        "search and conversation history to provide factual, helpful responses."
    ),
    system_prompt=_SYSTEM_PROMPT,
    capabilities=[
        "general_qa",
        "conversation",
    ],
    tools=["document_search", "calculator"],
    required_role=UserRole.VIEWER,  # Available to all users
    model_preference=None,  # Use default model
    max_tokens=2048,
    temperature=0.7,  # Standard temperature for conversation
    classification_access=["class_i", "class_ii"],
    requires_verification=False,  # General Q&A doesn't need verification
    metadata={
        "version": "1.0.0",
        "specialization": "general_purpose",
        "default_agent": True,
    },
)
