"""Base classes and types for the skills framework.

Skills are self-describing modular capabilities that agents can invoke.
Each skill declares its requirements (tools, role, classification access)
and provides a structured execution interface with full context.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from src.models.user import UserRole


@dataclass
class SkillManifest:
    """Self-describing skill metadata.

    Every skill must provide a manifest that declares:
    - What it can do (capabilities)
    - What it needs (tools, role)
    - What data it can access (classification levels)
    - Whether execution requires audit logging

    This enables the system to:
    1. Discover available skills dynamically
    2. Enforce access control before invocation
    3. Route to appropriate skills based on capabilities
    4. Audit sensitive operations
    """
    skill_id: str
    name: str
    description: str
    version: str
    capabilities: list[str]  # What this skill can do (e.g., ["document_analysis", "comparison"])
    required_tools: list[str]  # Tools this skill needs (e.g., ["document_search"])
    required_role: UserRole  # Minimum role to invoke (VIEWER, OPERATOR, ADMIN)
    classification_access: list[str]  # Max classification levels (e.g., ["class_i", "class_ii"])
    audit_required: bool  # Whether to audit every invocation
    parameters_schema: dict[str, Any]  # JSON Schema for skill parameters

    def __post_init__(self) -> None:
        """Validate manifest after initialization."""
        if not self.skill_id:
            raise ValueError("skill_id cannot be empty")
        if not self.name:
            raise ValueError("name cannot be empty")
        if not self.capabilities:
            raise ValueError("Skill must declare at least one capability")
        if not self.version:
            raise ValueError("version cannot be empty")
        if not self.parameters_schema:
            raise ValueError("parameters_schema cannot be empty")


@dataclass
class SkillContext:
    """Runtime context for skill execution.

    Provides the skill with all necessary information about:
    - Who is invoking it (tenant, user, role)
    - What agent is requesting it
    - What RAG context is available
    - What data classification level applies

    This context enables skills to:
    1. Enforce access control dynamically
    2. Scope operations to the correct tenant
    3. Use RAG context for enhanced responses
    4. Apply classification-aware filtering
    """
    tenant_id: uuid.UUID
    user_id: uuid.UUID
    user_role: UserRole
    agent_id: str
    rag_context: str  # Pre-retrieved context from document search
    data_classification: str  # Classification level of current context (e.g., "class_ii")


@dataclass
class SkillResult:
    """Output from a skill execution.

    Structured result that includes:
    - Success/failure status
    - Content for the user
    - Structured data for programmatic access
    - Error details if applicable
    - Citations for RAG-enhanced responses
    - Metadata for audit trails
    """
    success: bool
    content: str  # Human-readable result
    data: dict[str, Any] | None = None  # Structured data for agents/APIs
    error: str | None = None  # Error message if success=False
    citations: list[dict[str, Any]] = field(default_factory=list)  # Source citations
    metadata: dict[str, Any] = field(default_factory=dict)  # Audit/telemetry metadata

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "content": self.content,
            "data": self.data,
            "error": self.error,
            "citations": self.citations,
            "metadata": self.metadata,
        }


class BaseSkill(ABC):
    """Abstract base class for all skills.

    Skills implement the execute() method, which takes validated parameters
    and a runtime context, then returns a structured result.

    Skills should:
    - Be stateless (context provides all needed state)
    - Validate parameters against their schema
    - Include citations when using RAG data
    - Return structured data alongside human content
    - Handle errors gracefully with descriptive messages
    """

    manifest: SkillManifest

    @abstractmethod
    async def execute(self, params: dict[str, Any], context: SkillContext) -> SkillResult:
        """Execute the skill with validated parameters.

        Args:
            params: Validated parameters matching manifest.parameters_schema
            context: Runtime context with tenant, user, agent, RAG data

        Returns:
            SkillResult with success status, content, and optional data/citations

        Raises:
            Should catch and return errors in SkillResult rather than raising
        """
