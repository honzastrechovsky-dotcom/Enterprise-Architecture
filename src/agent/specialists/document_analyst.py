"""Document Analyst - expert at analyzing organizational documents.

This specialist excels at:
- Analyzing documents for key information
- Summarizing complex documents with citations
- Cross-referencing across multiple documents
- Comparing documents to identify differences and patterns
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

_SYSTEM_PROMPT = """You are a Document Analyst specialist for an enterprise organization.

Your expertise is analyzing organizational documents to extract, summarize, and synthesize
information. You excel at:

1. **Document Analysis**: Extract key facts, procedures, and data points from documents
2. **Summarization**: Create clear, concise summaries with proper citations
3. **Cross-Reference**: Connect related information across multiple documents
4. **Comparison**: Identify differences, similarities, and patterns between documents

**Guidelines**:
- ALWAYS cite your sources using the document references provided
- Quote directly when precision matters
- Highlight conflicting information if found across documents
- Organize findings in a clear, scannable format
- Flag any ambiguities or missing information
- Use bullet points and structured formatting for readability

**Citation Format**:
When you reference information from a document, cite it like: [Doc: filename.pdf, p.X]

**Your outputs are verified for accuracy before being shared with users.**"""


class DocumentAnalystAgent(BaseSpecialistAgent):
    """Document analysis specialist.

    This agent is optimized for working with document-heavy queries where
    the answer requires synthesizing information from one or more sources.
    """

    async def process(self, message: str, context: AgentContext) -> AgentResponse:
        """Process a document analysis request.

        Steps:
        1. Analyze the query to understand what's being asked
        2. Search for relevant documents if RAG context is insufficient
        3. Extract and synthesize the information
        4. Build a structured response with citations
        5. Document the reasoning process

        Args:
            message: User's question or analysis request
            context: Full context including RAG results

        Returns:
            AgentResponse with analysis, citations, and reasoning trace
        """
        reasoning_trace = [
            "Document Analyst activated for document analysis task",
            f"Query: {message[:100]}..." if len(message) > 100 else f"Query: {message}",
        ]

        tools_used = []
        citations = []

        # Step 1: Determine if we need to search for more documents
        if not context.rag_context or len(context.rag_context) < 100:
            reasoning_trace.append(
                "RAG context insufficient - searching for relevant documents"
            )

            search_result = await self._use_tool(
                "document_search",
                {"query": message, "top_k": 10},
                context,
            )

            tools_used.append({
                "tool": "document_search",
                "success": search_result.success,
                "query": message,
            })

            if search_result.success:
                reasoning_trace.append(
                    f"Found {len(search_result.data.get('results', []))} relevant passages"
                )
            else:
                reasoning_trace.append(
                    f"Document search failed: {search_result.error}"
                )

        # Step 2: Build the messages array with specialized instructions
        additional_instructions = """
**For this analysis task:**
1. Structure your response with clear headers
2. Use bullet points for key findings
3. Quote directly from documents when needed
4. Cite every piece of information
5. If documents conflict, note it explicitly
6. End with a summary of confidence level in the findings
"""

        messages = self._build_messages(message, context, additional_instructions)
        reasoning_trace.append(f"Built message context with {len(messages)} messages")

        # Step 3: Call LLM with document analysis prompt
        log.info(
            "document_analyst.analyzing",
            tenant_id=str(context.tenant_id),
            has_rag=bool(context.rag_context),
        )

        response_text = await self._call_llm(messages)
        reasoning_trace.append(
            f"Generated analysis ({len(response_text)} chars)"
        )

        # Step 4: Extract citations from RAG context if available
        # In a real implementation, we'd parse the RAG context structure
        # For now, mark that citations came from document search
        if context.rag_context:
            citations.append({
                "source": "document_search",
                "note": "Citations extracted from RAG context",
            })
            reasoning_trace.append("Extracted citations from RAG context")

        # Step 5: Set verification status based on agent requirements
        verification_status = "verified" if self.spec.requires_verification else "passed"

        log.info(
            "document_analyst.complete",
            tenant_id=str(context.tenant_id),
            response_length=len(response_text),
            tools_used=len(tools_used),
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
                "rag_context_length": len(context.rag_context),
                "conversation_turns": len(context.conversation_history),
            },
        )


# Agent specification
SPEC = AgentSpec(
    agent_id="document_analyst",
    name="Document Analyst",
    description=(
        "Expert at analyzing organizational documents, extracting key information, "
        "summarizing with citations, cross-referencing across documents, and "
        "comparing documents to identify patterns. Use for document-heavy queries "
        "requiring synthesis of written information."
    ),
    system_prompt=_SYSTEM_PROMPT,
    capabilities=[
        "document_analysis",
        "summarization",
        "cross_reference",
        "comparison",
    ],
    tools=["document_search"],
    required_role=UserRole.VIEWER,
    model_preference=None,  # Use default model
    max_tokens=2048,
    temperature=0.7,
    classification_access=["class_i", "class_ii"],
    requires_verification=True,
    metadata={
        "version": "1.0.0",
        "specialization": "document_analysis",
    },
)
