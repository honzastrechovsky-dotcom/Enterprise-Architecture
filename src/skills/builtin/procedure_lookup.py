"""Procedure lookup skill - SOP search and step-by-step guidance.

This skill provides structured access to standard operating procedures,
safety protocols, and maintenance instructions. Results include:
- Step-by-step instructions with verification checkpoints
- Safety warnings and prerequisites
- Related procedures and references
- Source citations for traceability

This skill requires verification (audit trail) for compliance reasons.
"""

from __future__ import annotations

from typing import Any

import structlog

from src.agent.llm import LLMClient
from src.agent.tools import ToolContext, ToolGateway, ToolResult
from src.models.user import UserRole
from src.skills.base import BaseSkill, SkillContext, SkillManifest, SkillResult

log = structlog.get_logger(__name__)


class ProcedureLookupSkill(BaseSkill):
    """SOP search with step-by-step guidance and safety checks.

    Retrieves and formats standard operating procedures, safety protocols,
    and maintenance instructions. Emphasizes safety warnings, prerequisites,
    and verification checkpoints.

    All lookups are audited for compliance tracking.
    """

    def __init__(self, llm_client: LLMClient | None = None, tool_gateway: ToolGateway | None = None) -> None:
        """Initialize procedure lookup skill.

        Args:
            llm_client: LLM client for procedure formatting (injected or created)
            tool_gateway: Tool gateway for document search (injected or created)
        """
        self._llm_client = llm_client or LLMClient()
        self._tool_gateway = tool_gateway or ToolGateway()

        self.manifest = SkillManifest(
            skill_id="procedure_lookup",
            name="Procedure Lookup",
            description=(
                "Search and retrieve standard operating procedures, safety protocols, "
                "and maintenance instructions. Provides step-by-step guidance with "
                "safety warnings and verification checkpoints."
            ),
            version="1.0.0",
            capabilities=["procedure_search", "sop_lookup", "safety_protocols", "maintenance_guidance"],
            required_tools=["document_search"],
            required_role=UserRole.OPERATOR,  # Operators and above can access procedures
            classification_access=["class_i", "class_ii", "class_iii"],
            audit_required=True,  # Always audit procedure lookups for compliance
            parameters_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The procedure or task to look up (e.g., 'startup procedure for reactor', 'emergency shutdown')",
                    },
                    "procedure_type": {
                        "type": "string",
                        "enum": ["sop", "safety", "maintenance"],
                        "description": "Type of procedure: sop (standard operating), safety (safety protocol), maintenance (maintenance task)",
                        "default": "sop",
                    },
                    "include_related": {
                        "type": "boolean",
                        "description": "Include related procedures and cross-references (default: true)",
                        "default": True,
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, params: dict[str, Any], context: SkillContext) -> SkillResult:
        """Execute procedure lookup with structured guidance.

        Args:
            params: Validated parameters (query, procedure_type, include_related)
            context: Runtime context with tenant, user, agent, RAG data

        Returns:
            SkillResult with formatted procedure steps and safety warnings
        """
        query = params["query"]
        procedure_type = params.get("procedure_type", "sop")
        include_related = params.get("include_related", True)

        log.info(
            "skill.procedure_lookup.executing",
            tenant_id=str(context.tenant_id),
            user_id=str(context.user_id),
            user_role=context.user_role,
            query=query,
            procedure_type=procedure_type,
        )

        try:
            # Step 1: Build type-specific search query
            enhanced_query = self._enhance_query(query, procedure_type)

            # Step 2: Retrieve relevant procedure documents
            search_result = await self._search_procedures(
                query=enhanced_query,
                top_k=15,  # More results for comprehensive procedure coverage
                context=context,
            )

            if not search_result.success:
                return SkillResult(
                    success=False,
                    content="",
                    error=f"Procedure search failed: {search_result.error}",
                )

            passages = search_result.data.get("passages", [])
            if not passages:
                return SkillResult(
                    success=True,
                    content=f"No {procedure_type} procedures found matching: {query}",
                    data={"passages": []},
                    citations=[],
                    metadata={"audit_note": "No procedures found"},
                )

            # Step 3: Format procedure with LLM
            system_prompt = self._build_system_prompt(procedure_type)
            user_prompt = self._build_user_prompt(query, passages, include_related)

            response = await self._llm_client.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,  # Very low temperature for procedural accuracy
                max_tokens=3000,
            )

            procedure_text = self._llm_client.extract_text(response)

            # Step 4: Extract citations
            citations = [
                {
                    "document_id": p.get("document_id"),
                    "document_name": p.get("document_name"),
                    "page": p.get("page"),
                    "section": p.get("section"),
                    "procedure_type": procedure_type,
                    "passage": p.get("text", "")[:200] + "...",
                }
                for p in passages[:5]  # Top 5 most relevant
            ]

            log.info(
                "skill.procedure_lookup.completed",
                tenant_id=str(context.tenant_id),
                user_id=str(context.user_id),
                procedure_type=procedure_type,
                passages_used=len(passages),
            )

            return SkillResult(
                success=True,
                content=procedure_text,
                data={
                    "query": query,
                    "procedure_type": procedure_type,
                    "passages_analyzed": len(passages),
                    "related_procedures_included": include_related,
                },
                citations=citations,
                metadata={
                    "skill_id": self.manifest.skill_id,
                    "version": self.manifest.version,
                    "agent_id": context.agent_id,
                    "audit_note": f"Procedure lookup: {procedure_type} - {query}",
                    "classification": context.data_classification,
                },
            )

        except Exception as exc:
            log.error(
                "skill.procedure_lookup.failed",
                tenant_id=str(context.tenant_id),
                error=str(exc),
            )
            return SkillResult(
                success=False,
                content="",
                error=f"Procedure lookup failed: {exc}",
                metadata={"audit_note": f"Failed lookup: {query}"},
            )

    async def _search_procedures(self, query: str, top_k: int, context: SkillContext) -> ToolResult:
        """Call document_search tool for procedure documents."""
        tool_context = ToolContext(
            tenant_id=str(context.tenant_id),
            user_id=str(context.user_id),
            user_role=context.user_role,
        )

        return await self._tool_gateway.execute(
            tool_name="document_search",
            params={"query": query, "top_k": top_k},
            context=tool_context,
        )

    def _enhance_query(self, query: str, procedure_type: str) -> str:
        """Enhance query with type-specific keywords."""
        type_keywords = {
            "sop": "standard operating procedure SOP",
            "safety": "safety protocol warning hazard emergency",
            "maintenance": "maintenance procedure checklist inspection",
        }

        keywords = type_keywords.get(procedure_type, "")
        return f"{query} {keywords}"

    def _build_system_prompt(self, procedure_type: str) -> str:
        """Build system prompt for procedure formatting."""
        base = (
            "You are a procedure specialist. Your role is to extract and format procedures "
            "from provided documentation into clear, step-by-step guidance. "
            "Emphasize safety warnings, prerequisites, and verification checkpoints. "
            "Only include information explicitly stated in the source documents."
        )

        type_specific = {
            "sop": (
                "\n\nFor SOPs: Format as numbered steps with substeps. Include prerequisites, "
                "required tools/materials, safety considerations, and verification checkpoints. "
                "Note any quality checks or sign-offs required."
            ),
            "safety": (
                "\n\nFor safety protocols: Lead with hazard warnings and PPE requirements. "
                "Format emergency procedures with clear decision points. Include evacuation "
                "routes, communication protocols, and emergency contacts if mentioned."
            ),
            "maintenance": (
                "\n\nFor maintenance: List tools and parts needed upfront. Format as numbered "
                "steps with torque specs, clearances, and inspection criteria. Include "
                "troubleshooting guidance and post-maintenance verification steps."
            ),
        }

        return base + type_specific.get(procedure_type, "")

    def _build_user_prompt(self, query: str, passages: list[dict[str, Any]], include_related: bool) -> str:
        """Build user prompt with query and procedure passages."""
        passages_text = "\n\n---\n\n".join(
            [
                f"Document: {p.get('document_name', 'Unknown')} (Page {p.get('page', 'N/A')})\n{p.get('text', '')}"
                for p in passages
            ]
        )

        related_instruction = ""
        if include_related:
            related_instruction = (
                "\n\nIf the passages mention related procedures, prerequisites, or "
                "follow-up tasks, include a 'Related Procedures' section at the end."
            )

        return f"""Query: {query}

Relevant Procedure Documentation:

{passages_text}

---

Based on the above documentation, provide a clear, step-by-step procedure that addresses the query.
Format with:
1. Prerequisites and Safety Warnings (if any)
2. Required Tools/Materials (if mentioned)
3. Step-by-Step Instructions (numbered)
4. Verification/Quality Checks (if specified)
{related_instruction}

Be precise and only include information from the source documents."""
