"""Document analysis skill - deep analysis, comparison, and summarization.

This skill provides sophisticated document understanding capabilities using
LLM reasoning combined with RAG retrieval. It can:
- Summarize single or multiple documents
- Compare documents for differences, similarities, overlaps
- Extract structured information with citations
- Answer complex questions requiring synthesis across sources
"""

from __future__ import annotations

from typing import Any

import structlog

from src.agent.llm import LLMClient
from src.agent.tools import ToolContext, ToolGateway, ToolResult
from src.models.user import UserRole
from src.skills.base import BaseSkill, SkillContext, SkillManifest, SkillResult

log = structlog.get_logger(__name__)


class DocumentAnalysisSkill(BaseSkill):
    """Deep document analysis with citations.

    Uses the LLM to synthesize information from multiple documents,
    compare content, extract key information, or answer complex questions
    that require understanding and reasoning over document content.

    All results include citations back to source documents for verification.
    """

    def __init__(self, llm_client: LLMClient | None = None, tool_gateway: ToolGateway | None = None) -> None:
        """Initialize document analysis skill.

        Args:
            llm_client: LLM client for reasoning (injected or created)
            tool_gateway: Tool gateway for document search (injected or created)
        """
        self._llm_client = llm_client or LLMClient()
        self._tool_gateway = tool_gateway or ToolGateway()

        self.manifest = SkillManifest(
            skill_id="document_analysis",
            name="Document Analysis",
            description=(
                "Deep analysis of documents including summarization, comparison, "
                "information extraction, and synthesis across multiple sources. "
                "All results include citations."
            ),
            version="1.0.0",
            capabilities=["document_analysis", "summarization", "comparison", "extraction"],
            required_tools=["document_search"],
            required_role=UserRole.VIEWER,
            classification_access=["class_i", "class_ii", "class_iii"],
            audit_required=False,
            parameters_schema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The analysis question or task (e.g., 'Summarize safety procedures', 'Compare maintenance schedules')",
                    },
                    "documents": {
                        "type": "array",
                        "description": "Optional list of specific document IDs to analyze. If not provided, searches all accessible documents.",
                        "items": {"type": "string"},
                        "default": [],
                    },
                    "analysis_type": {
                        "type": "string",
                        "enum": ["summary", "compare", "extract"],
                        "description": "Type of analysis: summary (condense), compare (find differences), extract (structured data)",
                        "default": "summary",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of document passages to retrieve (default: 10)",
                        "default": 10,
                        "minimum": 1,
                        "maximum": 50,
                    },
                },
                "required": ["query"],
            },
        )

    async def execute(self, params: dict[str, Any], context: SkillContext) -> SkillResult:
        """Execute document analysis with citations.

        Args:
            params: Validated parameters (query, documents, analysis_type, top_k)
            context: Runtime context with tenant, user, agent, RAG data

        Returns:
            SkillResult with analysis content and source citations
        """
        query = params["query"]
        analysis_type = params.get("analysis_type", "summary")
        top_k = params.get("top_k", 10)

        log.info(
            "skill.document_analysis.executing",
            tenant_id=str(context.tenant_id),
            user_id=str(context.user_id),
            query=query,
            analysis_type=analysis_type,
        )

        try:
            # Step 1: Retrieve relevant document passages
            search_result = await self._search_documents(
                query=query,
                top_k=top_k,
                context=context,
            )

            if not search_result.success:
                return SkillResult(
                    success=False,
                    content="",
                    error=f"Document search failed: {search_result.error}",
                )

            passages = search_result.data.get("passages", [])
            if not passages:
                return SkillResult(
                    success=True,
                    content="No relevant documents found for this query.",
                    data={"passages": []},
                    citations=[],
                )

            # Step 2: Build analysis prompt based on type
            system_prompt = self._build_system_prompt(analysis_type)
            user_prompt = self._build_user_prompt(query, passages, analysis_type)

            # Step 3: Call LLM for analysis
            response = await self._llm_client.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,  # Lower temperature for factual analysis
                max_tokens=2048,
            )

            analysis_text = self._llm_client.extract_text(response)

            # Step 4: Extract citations from passages
            citations = [
                {
                    "document_id": p.get("document_id"),
                    "document_name": p.get("document_name"),
                    "page": p.get("page"),
                    "passage": p.get("text", "")[:200] + "...",  # First 200 chars
                    "relevance_score": p.get("score"),
                }
                for p in passages
            ]

            log.info(
                "skill.document_analysis.completed",
                tenant_id=str(context.tenant_id),
                passages_used=len(passages),
                citations_count=len(citations),
            )

            return SkillResult(
                success=True,
                content=analysis_text,
                data={
                    "query": query,
                    "analysis_type": analysis_type,
                    "passages_analyzed": len(passages),
                },
                citations=citations,
                metadata={
                    "skill_id": self.manifest.skill_id,
                    "version": self.manifest.version,
                    "agent_id": context.agent_id,
                },
            )

        except Exception as exc:
            log.error(
                "skill.document_analysis.failed",
                tenant_id=str(context.tenant_id),
                error=str(exc),
            )
            return SkillResult(
                success=False,
                content="",
                error=f"Document analysis failed: {exc}",
            )

    async def _search_documents(self, query: str, top_k: int, context: SkillContext) -> ToolResult:
        """Call document_search tool to retrieve relevant passages."""
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

    def _build_system_prompt(self, analysis_type: str) -> str:
        """Build system prompt based on analysis type."""
        base = (
            "You are a document analyst. Your role is to analyze provided document passages "
            "and synthesize a clear, accurate response. Always base your answer strictly on "
            "the provided passages. If the passages don't contain enough information, say so."
        )

        type_specific = {
            "summary": (
                "\n\nFor summarization: Create a concise summary that captures key points, "
                "maintains accuracy, and preserves important details."
            ),
            "compare": (
                "\n\nFor comparison: Identify similarities and differences between the documents. "
                "Highlight areas of agreement, contradiction, and complementary information."
            ),
            "extract": (
                "\n\nFor extraction: Pull out specific structured information requested in the query. "
                "Format as clear bullet points or structured data. Include evidence from passages."
            ),
        }

        return base + type_specific.get(analysis_type, "")

    def _build_user_prompt(self, query: str, passages: list[dict[str, Any]], analysis_type: str) -> str:
        """Build user prompt with query and document passages."""
        passages_text = "\n\n---\n\n".join(
            [
                f"Document: {p.get('document_name', 'Unknown')} (Page {p.get('page', 'N/A')})\n{p.get('text', '')}"
                for p in passages
            ]
        )

        return f"""Query: {query}

Relevant Document Passages:

{passages_text}

---

Based on the above passages, provide a {analysis_type} that answers the query. Include specific references to source documents where appropriate."""
