"""Base classes for specialist agents.

All specialist agents inherit from BaseSpecialistAgent and implement
the process() method with their domain-specific logic.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import structlog

from src.agent.llm import LLMClient
from src.agent.registry import AgentSpec
from src.agent.tools import ToolContext, ToolGateway, ToolResult
from src.models.user import UserRole

log = structlog.get_logger(__name__)


@dataclass
class AgentContext:
    """Context passed to specialist agents for each request.

    This contains all the information an agent needs to process a request:
    tenant/user identification, conversation history, RAG context, and
    security/access information.
    """
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    user_role: UserRole
    conversation_id: uuid.UUID | None
    rag_context: str
    conversation_history: list[dict[str, str]]
    data_classification_level: str = "class_ii"


@dataclass
class AgentResponse:
    """Response from a specialist agent.

    This captures not just the content but also metadata about how the
    agent arrived at the response - citations, tool usage, reasoning steps,
    and verification status.
    """
    content: str
    agent_id: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    tools_used: list[dict[str, Any]] = field(default_factory=list)
    reasoning_trace: list[str] = field(default_factory=list)
    verification_status: str = "pending"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "content": self.content,
            "agent_id": self.agent_id,
            "citations": self.citations,
            "tools_used": self.tools_used,
            "reasoning_trace": self.reasoning_trace,
            "verification_status": self.verification_status,
            "metadata": self.metadata,
        }


class BaseSpecialistAgent(ABC):
    """Abstract base class for all specialist agents.

    Specialist agents implement domain-specific processing logic while
    sharing common infrastructure for LLM calls, tool usage, and logging.

    Each specialist must:
    1. Provide an AgentSpec describing its capabilities
    2. Implement process() with domain logic
    3. Use _call_llm() and _use_tool() for consistency
    """

    def __init__(
        self,
        spec: AgentSpec,
        llm_client: LLMClient,
        tool_gateway: ToolGateway,
    ) -> None:
        """Initialize the specialist agent.

        Args:
            spec: The agent specification (capabilities, tools, etc.)
            llm_client: Client for making LLM calls
            tool_gateway: Gateway for executing tools with access control
        """
        self.spec = spec
        self._llm = llm_client
        self._tools = tool_gateway

    @abstractmethod
    async def process(
        self,
        message: str,
        context: AgentContext,
    ) -> AgentResponse:
        """Process a message using this specialist's domain expertise.

        This is the main entry point for specialist agents. Implementations
        should:
        1. Use the agent's system prompt
        2. Incorporate RAG context and conversation history
        3. Call tools as needed
        4. Build a structured reasoning trace
        5. Return an AgentResponse with verification status

        Args:
            message: The user's message
            context: Full context (tenant, user, history, RAG, etc.)

        Returns:
            AgentResponse with content, citations, reasoning, etc.
        """

    async def _call_llm(
        self,
        messages: list[dict[str, str]],
        **kwargs: Any,
    ) -> str:
        """Make an LLM call using this agent's configuration.

        Uses the agent's model preference, temperature, and max_tokens
        from the spec unless overridden in kwargs.

        Args:
            messages: List of role/content dicts for the LLM
            **kwargs: Override spec defaults (temperature, max_tokens, etc.)

        Returns:
            The assistant's response text
        """
        # Apply agent spec defaults, allow kwargs to override
        effective_kwargs = {
            "model": self.spec.model_preference,
            "temperature": self.spec.temperature,
            "max_tokens": self.spec.max_tokens,
        }
        effective_kwargs.update(kwargs)

        log.debug(
            "specialist.llm_call",
            agent_id=self.spec.agent_id,
            message_count=len(messages),
            **effective_kwargs,
        )

        response = await self._llm.complete(messages=messages, **effective_kwargs)
        text = self._llm.extract_text(response)

        return text

    async def _use_tool(
        self,
        tool_name: str,
        params: dict[str, Any],
        context: AgentContext,
    ) -> ToolResult:
        """Execute a tool with access control.

        This wraps the tool gateway to provide consistent logging and
        error handling for specialist agents.

        Args:
            tool_name: Name of the tool to execute
            params: Parameters for the tool
            context: Agent context (for tenant/user/role)

        Returns:
            ToolResult with success status and data/error
        """
        tool_context = ToolContext(
            tenant_id=str(context.tenant_id),
            user_id=str(context.user_id),
            user_role=context.user_role,
        )

        log.info(
            "specialist.tool_call",
            agent_id=self.spec.agent_id,
            tool=tool_name,
            tenant_id=str(context.tenant_id),
        )

        result = await self._tools.execute(tool_name, params, tool_context)

        if not result.success:
            log.warning(
                "specialist.tool_failed",
                agent_id=self.spec.agent_id,
                tool=tool_name,
                error=result.error,
            )

        return result

    def _build_messages(
        self,
        message: str,
        context: AgentContext,
        additional_instructions: str = "",
    ) -> list[dict[str, str]]:
        """Build the messages array for an LLM call.

        Combines:
        - Agent's system prompt
        - RAG context if available
        - Additional instructions (optional)
        - Conversation history
        - Current message

        Args:
            message: The current user message
            context: Full agent context
            additional_instructions: Extra instructions to append to system prompt

        Returns:
            List of role/content dicts ready for LLM
        """
        system_content = self.spec.system_prompt

        if context.rag_context:
            system_content += (
                f"\n\nRelevant context from documents:\n{context.rag_context}"
            )

        if additional_instructions:
            system_content += f"\n\n{additional_instructions}"

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_content},
        ]

        # Add conversation history
        messages.extend(context.conversation_history)

        # Add current message
        messages.append({"role": "user", "content": message})

        return messages
