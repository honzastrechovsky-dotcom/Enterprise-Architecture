"""Report generation skill - structured reports from multiple data sources.

This skill synthesizes information from multiple sources (documents, tools,
RAG context) into formatted reports. Supports:
- Executive summaries
- Technical reports with data tables
- Compliance reports with citations
- Custom report templates
- Multi-section structure with configurable content

All AI-generated reports include an AI disclosure footer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import structlog

from src.agent.llm import LLMClient
from src.agent.tools import ToolContext, ToolGateway, ToolResult
from src.models.user import UserRole
from src.skills.base import BaseSkill, SkillContext, SkillManifest, SkillResult

log = structlog.get_logger(__name__)


class ReportGenerationSkill(BaseSkill):
    """Generate structured reports from multiple data sources.

    Creates formatted reports by synthesizing information from RAG retrieval,
    tool outputs, and structured templates. Includes section management,
    citation tracking, and AI disclosure.
    """

    # Report templates define section structure
    TEMPLATES = {
        "executive": ["Executive Summary", "Key Findings", "Recommendations", "Next Steps"],
        "technical": ["Overview", "Technical Details", "Data Analysis", "Conclusions", "References"],
        "compliance": ["Compliance Status", "Findings", "Non-Conformances", "Corrective Actions", "Citations"],
        "incident": ["Incident Summary", "Timeline", "Root Cause", "Impact Assessment", "Response Actions", "Lessons Learned"],
        "custom": [],  # User-defined sections
    }

    def __init__(self, llm_client: LLMClient | None = None, tool_gateway: ToolGateway | None = None) -> None:
        """Initialize report generation skill.

        Args:
            llm_client: LLM client for content generation (injected or created)
            tool_gateway: Tool gateway for data retrieval (injected or created)
        """
        self._llm_client = llm_client or LLMClient()
        self._tool_gateway = tool_gateway or ToolGateway()

        self.manifest = SkillManifest(
            skill_id="report_generation",
            name="Report Generation",
            description=(
                "Generate structured reports from multiple data sources. "
                "Supports executive summaries, technical reports, compliance reports, "
                "and custom templates. All reports include AI disclosure."
            ),
            version="1.0.0",
            capabilities=["report_generation", "documentation", "synthesis"],
            required_tools=["document_search"],
            required_role=UserRole.OPERATOR,  # Report generation requires operator role
            classification_access=["class_i", "class_ii", "class_iii"],
            audit_required=True,  # Audit all report generation for tracking
            parameters_schema={
                "type": "object",
                "properties": {
                    "report_type": {
                        "type": "string",
                        "enum": ["executive", "technical", "compliance", "incident", "custom"],
                        "description": "Type of report template to use",
                    },
                    "title": {
                        "type": "string",
                        "description": "Report title",
                    },
                    "sections": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Custom section names (for 'custom' report_type). Overrides template.",
                        "default": [],
                    },
                    "data_sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of queries to retrieve data for each section (one per section, or general queries)",
                        "default": [],
                    },
                    "include_citations": {
                        "type": "boolean",
                        "description": "Include source citations in the report (default: true)",
                        "default": True,
                    },
                },
                "required": ["report_type", "title"],
            },
        )

    async def execute(self, params: dict[str, Any], context: SkillContext) -> SkillResult:
        """Execute report generation with structured sections.

        Args:
            params: Validated parameters (report_type, title, sections, data_sources, include_citations)
            context: Runtime context with tenant, user, agent, RAG data

        Returns:
            SkillResult with formatted report and citations
        """
        report_type = params["report_type"]
        title = params["title"]
        custom_sections = params.get("sections", [])
        data_sources = params.get("data_sources", [])
        include_citations = params.get("include_citations", True)

        log.info(
            "skill.report_generation.executing",
            tenant_id=str(context.tenant_id),
            user_id=str(context.user_id),
            report_type=report_type,
            title=title,
        )

        try:
            # Step 1: Determine section structure
            if report_type == "custom" and custom_sections:
                sections = custom_sections
            else:
                sections = self.TEMPLATES.get(report_type, self.TEMPLATES["technical"])

            if not sections:
                return SkillResult(
                    success=False,
                    content="",
                    error="Report must have at least one section. Provide 'sections' parameter for custom reports.",
                )

            # Step 2: Retrieve data for report sections
            all_passages = []
            for query in data_sources:
                search_result = await self._search_data(query, context)
                if search_result.success:
                    passages = search_result.data.get("passages", [])
                    all_passages.extend(passages)

            # Step 3: Generate report content using LLM
            system_prompt = self._build_system_prompt(report_type)
            user_prompt = self._build_user_prompt(title, sections, all_passages, context.rag_context)

            response = await self._llm_client.complete(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.4,  # Moderate temperature for coherent narrative
                max_tokens=4000,  # Longer output for full reports
            )

            report_content = self._llm_client.extract_text(response)

            # Step 4: Format full report with header and footer
            report_text = self._format_report(
                title=title,
                content=report_content,
                include_citations=include_citations,
                citations=all_passages,
                context=context,
            )

            # Step 5: Extract citation metadata
            citations = []
            if include_citations:
                citations = [
                    {
                        "document_id": p.get("document_id"),
                        "document_name": p.get("document_name"),
                        "page": p.get("page"),
                        "relevance_score": p.get("score"),
                    }
                    for p in all_passages[:10]  # Top 10 sources
                ]

            log.info(
                "skill.report_generation.completed",
                tenant_id=str(context.tenant_id),
                user_id=str(context.user_id),
                report_type=report_type,
                sections_count=len(sections),
                citations_count=len(citations),
            )

            return SkillResult(
                success=True,
                content=report_text,
                data={
                    "title": title,
                    "report_type": report_type,
                    "sections": sections,
                    "data_sources_queried": len(data_sources),
                    "passages_used": len(all_passages),
                },
                citations=citations,
                metadata={
                    "skill_id": self.manifest.skill_id,
                    "version": self.manifest.version,
                    "agent_id": context.agent_id,
                    "audit_note": f"Report generated: {report_type} - {title}",
                    "classification": context.data_classification,
                },
            )

        except Exception as exc:
            log.error(
                "skill.report_generation.failed",
                tenant_id=str(context.tenant_id),
                error=str(exc),
            )
            return SkillResult(
                success=False,
                content="",
                error=f"Report generation failed: {exc}",
                metadata={"audit_note": f"Failed report: {title}"},
            )

    async def _search_data(self, query: str, context: SkillContext) -> ToolResult:
        """Retrieve data using document search tool."""
        tool_context = ToolContext(
            tenant_id=str(context.tenant_id),
            user_id=str(context.user_id),
            user_role=context.user_role,
        )

        return await self._tool_gateway.execute(
            tool_name="document_search",
            params={"query": query, "top_k": 10},
            context=tool_context,
        )

    def _build_system_prompt(self, report_type: str) -> str:
        """Build system prompt for report generation."""
        base = (
            "You are a professional report writer. Generate clear, well-structured reports "
            "based on provided data and context. Use formal business language, maintain "
            "objectivity, and organize information logically under the specified sections."
        )

        type_specific = {
            "executive": (
                "\n\nFor executive reports: Be concise and focus on high-level insights, "
                "business impact, and actionable recommendations. Avoid excessive technical detail."
            ),
            "technical": (
                "\n\nFor technical reports: Include detailed analysis, data, and technical "
                "specifications. Use precise terminology and support claims with evidence."
            ),
            "compliance": (
                "\n\nFor compliance reports: Emphasize adherence to standards, identify gaps, "
                "and provide clear corrective action plans. Include regulatory references."
            ),
            "incident": (
                "\n\nFor incident reports: Present chronological timeline, root cause analysis, "
                "impact assessment, and lessons learned. Be factual and avoid speculation."
            ),
        }

        return base + type_specific.get(report_type, "")

    def _build_user_prompt(
        self,
        title: str,
        sections: list[str],
        passages: list[dict[str, Any]],
        rag_context: str,
    ) -> str:
        """Build user prompt with report structure and data."""
        sections_text = "\n".join([f"- {section}" for section in sections])

        passages_text = ""
        if passages:
            passages_text = "\n\n---\n\nRelevant Data Sources:\n\n"
            passages_text += "\n\n".join(
                [
                    f"Source: {p.get('document_name', 'Unknown')}\n{p.get('text', '')}"
                    for p in passages[:15]  # Limit to avoid token overflow
                ]
            )

        context_text = ""
        if rag_context:
            context_text = f"\n\nAdditional Context:\n{rag_context}"

        return f"""Generate a report with the following structure:

Title: {title}

Sections:
{sections_text}
{passages_text}
{context_text}

---

Write a complete, well-structured report covering all sections. Base content on the provided data sources.
If data is insufficient for a section, note that explicitly. Do not fabricate information."""

    def _format_report(
        self,
        title: str,
        content: str,
        include_citations: bool,
        citations: list[dict[str, Any]],
        context: SkillContext,
    ) -> str:
        """Format full report with header, content, citations, and footer."""
        timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

        # Header
        report = f"""{'=' * 80}
{title.upper().center(80)}
{'=' * 80}

Generated: {timestamp}
Classification: {context.data_classification.upper()}
Prepared by: AI Agent ({context.agent_id})

"""

        # Content
        report += content

        # Citations section
        if include_citations and citations:
            report += "\n\n" + "=" * 80 + "\n"
            report += "REFERENCES\n"
            report += "=" * 80 + "\n\n"

            for idx, cite in enumerate(citations, 1):
                doc_name = cite.get("document_name", "Unknown")
                page = cite.get("page", "N/A")
                report += f"[{idx}] {doc_name}, Page {page}\n"

        # AI Disclosure footer
        report += "\n\n" + "=" * 80 + "\n"
        report += "AI DISCLOSURE: This report was generated by an AI system using data from\n"
        report += "organizational documents. All information should be verified by qualified\n"
        report += "personnel before making critical decisions.\n"
        report += "=" * 80 + "\n"

        return report
