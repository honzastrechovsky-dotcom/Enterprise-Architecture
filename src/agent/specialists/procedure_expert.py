"""Procedure Expert - specialist in SOPs, safety procedures, and manufacturing processes.

This specialist excels at:
- Looking up standard operating procedures (SOPs)
- Providing step-by-step guidance for processes
- Referencing safety procedures and requirements
- Breaking down complex procedures into clear steps
"""

from __future__ import annotations

import structlog

from src.agent.reasoning import ReasoningEngine
from src.agent.registry import AgentSpec
from src.agent.specialists.base import (
    AgentContext,
    AgentResponse,
    BaseSpecialistAgent,
)
from src.config import get_settings
from src.models.user import UserRole

log = structlog.get_logger(__name__)

_SYSTEM_PROMPT = """You are a Procedure Expert specialist for an enterprise organization.

Your expertise is in standard operating procedures (SOPs), safety protocols, and
manufacturing processes. You provide step-by-step guidance to help workers follow
procedures correctly and safely.

**Your core responsibilities:**

1. **SOP Lookup**: Find and reference the correct procedure documents
2. **Step-by-Step Guidance**: Break down procedures into clear, numbered steps
3. **Safety Warnings**: Highlight safety-critical steps and required PPE
4. **Prerequisites**: Identify what must be completed before starting
5. **Quality Checkpoints**: Note verification steps and quality gates

**Critical Safety Rules:**
- ⚠️ ALWAYS highlight safety-critical steps with a warning symbol
- ⚠️ List required Personal Protective Equipment (PPE) at the start
- ⚠️ Note STOP points where supervisor verification is required
- ⚠️ Reference emergency procedures if relevant
- ⚠️ Flag any deviations from standard procedures

**Format for Procedure Responses:**
1. **Procedure**: [Name and document reference]
2. **Safety Requirements**: [PPE, precautions, hazards]
3. **Prerequisites**: [What must be done first]
4. **Steps**: [Numbered, clear steps with checkpoints]
5. **Verification**: [How to confirm successful completion]

**Important**: All safety-critical procedures REQUIRE human verification before execution.
Your guidance is reference material only - workers must follow official SOPs and
supervisor guidance.

**Citation Format**: [SOP: document-id, section X.Y]"""


class ProcedureExpertAgent(BaseSpecialistAgent):
    """Procedure and safety specialist.

    This agent is optimized for queries about how to perform procedures,
    what safety requirements apply, and step-by-step guidance for operations.
    """

    async def process(self, message: str, context: AgentContext) -> AgentResponse:
        """Process a procedure-related query.

        Steps:
        1. Execute ReasoningEngine OBSERVE→THINK→VERIFY cycle
        2. Identify the procedure being asked about
        3. Search for the relevant SOP documents
        4. Extract step-by-step instructions
        5. Highlight safety-critical elements
        6. Structure as clear, actionable guidance

        Args:
            message: User's question about a procedure
            context: Full context including RAG results

        Returns:
            AgentResponse with procedure guidance and safety warnings
        """
        reasoning_trace = [
            "Procedure Expert activated for SOP/safety guidance",
            f"Query: {message[:100]}..." if len(message) > 100 else f"Query: {message}",
        ]

        tools_used = []
        citations = []

        # Step 0: Execute structured reasoning (OBSERVE→THINK→VERIFY)
        log.info(
            "procedure_expert.reasoning_start",
            tenant_id=str(context.tenant_id),
        )

        engine = ReasoningEngine(self._llm, get_settings())
        reasoning_result = await engine.reason(
            query=message,
            context=context.rag_context or "",
            agent_spec=self.spec,
            require_verification=True,
        )

        reasoning_trace.extend([
            f"OBSERVE: {len(reasoning_result.observation.key_facts)} key facts identified",
            f"THINK: {len(reasoning_result.reasoning_steps)} reasoning steps constructed",
            f"VERIFY: {'✓ verified' if reasoning_result.verification.is_verified else '✗ failed'}",
            f"Confidence: {reasoning_result.total_confidence:.2f}",
        ])

        log.info(
            "procedure_expert.reasoning_complete",
            tenant_id=str(context.tenant_id),
            confidence=reasoning_result.total_confidence,
            verified=reasoning_result.verification.is_verified,
            requires_review=reasoning_result.verification.requires_human_review,
        )

        # Step 1: Search for relevant procedure documents
        reasoning_trace.append("Searching for relevant SOPs and procedures")

        search_result = await self._use_tool(
            "document_search",
            {
                "query": f"procedure SOP safety: {message}",
                "top_k": 8,
            },
            context,
        )

        tools_used.append({
            "tool": "document_search",
            "success": search_result.success,
            "query_type": "procedure_lookup",
        })

        if search_result.success:
            reasoning_trace.append(
                "Found relevant procedure documents for analysis"
            )
        else:
            reasoning_trace.append(
                f"Procedure search failed: {search_result.error}"
            )
            reasoning_trace.append(
                "Proceeding with available RAG context only"
            )

        # Step 2: Build messages with safety-focused instructions (enriched with reasoning)
        additional_instructions = f"""
**CRITICAL: This is a procedure/safety query. Your response MUST:**

**REASONING CONTEXT (from structured analysis):**
- Conclusion: {reasoning_result.conclusion}
- Confidence: {reasoning_result.total_confidence:.2f}
- Verification: {"✓ Passed" if reasoning_result.verification.is_verified else "✗ Failed"}
{f"- Review Required: {reasoning_result.verification.review_reason}" if reasoning_result.verification.requires_human_review else ""}

1. **Start with Safety Requirements**:
   - Required PPE
   - Hazards and precautions
   - Emergency procedures (if applicable)

2. **Structure as Numbered Steps**:
   - One action per step
   - Use ⚠️ for safety-critical steps
   - Mark supervisor verification points with 🛑

3. **Include Quality Checkpoints**:
   - How to verify each step was done correctly
   - What to do if verification fails

4. **End with Completion Verification**:
   - How to confirm the procedure is complete
   - Required documentation or sign-offs

5. **Cite Every Procedure Reference**: Use [SOP: doc-name, section]

⚠️ IMPORTANT: Add this disclaimer at the end:
"This guidance is for reference only. Always follow official SOPs and supervisor direction.
Human verification required before executing safety-critical procedures."
"""

        messages = self._build_messages(message, context, additional_instructions)
        reasoning_trace.append(
            f"Built safety-focused message context ({len(messages)} messages)"
        )

        # Step 3: Call LLM with procedure expert prompt
        log.info(
            "procedure_expert.analyzing",
            tenant_id=str(context.tenant_id),
            has_rag=bool(context.rag_context),
        )

        response_text = await self._call_llm(messages)
        reasoning_trace.append(
            f"Generated procedure guidance ({len(response_text)} chars)"
        )

        # Step 4: Extract citations and flag as safety-critical
        if context.rag_context or search_result.success:
            citations.append({
                "source": "procedure_documents",
                "type": "safety_critical",
                "note": "References from SOP and safety documentation",
            })
            reasoning_trace.append("Extracted procedure citations from search results")

        # Step 5: Set verification status from reasoning result
        verification_status = "verified" if reasoning_result.verification.is_verified else "failed"
        if reasoning_result.verification.requires_human_review:
            verification_status = "pending_human_review"

        reasoning_trace.append(
            f"Verification: {verification_status} (safety-critical domain)"
        )

        log.info(
            "procedure_expert.complete",
            tenant_id=str(context.tenant_id),
            response_length=len(response_text),
            verification_required=True,
            verification_status=verification_status,
        )

        return AgentResponse(
            content=response_text,
            agent_id=self.spec.agent_id,
            citations=citations,
            tools_used=tools_used,
            reasoning_trace=reasoning_trace,
            verification_status=verification_status,
            metadata={
                "safety_critical": True,
                "requires_supervisor_approval": True,
                "procedure_type": "sop_guidance",
                "reasoning_confidence": reasoning_result.total_confidence,
                "reasoning_verified": reasoning_result.verification.is_verified,
                "reasoning_steps": len(reasoning_result.reasoning_steps),
            },
        )


# Agent specification
SPEC = AgentSpec(
    agent_id="procedure_expert",
    name="Procedure Expert",
    description=(
        "Specialist in SOPs, safety procedures, and manufacturing processes. "
        "Provides step-by-step guidance with safety warnings and quality checkpoints. "
        "Use for queries about how to perform procedures, safety requirements, "
        "or operational steps. All outputs require human verification."
    ),
    system_prompt=_SYSTEM_PROMPT,
    capabilities=[
        "procedure_lookup",
        "sop_guidance",
        "safety_reference",
        "step_by_step",
    ],
    tools=["document_search"],
    required_role=UserRole.OPERATOR,
    model_preference=None,
    max_tokens=2048,
    temperature=0.3,  # Lower temperature for procedure accuracy
    classification_access=["class_i", "class_ii"],
    requires_verification=True,  # ALWAYS require verification for safety
    metadata={
        "version": "1.0.0",
        "specialization": "procedures_and_safety",
        "safety_critical": True,
    },
)
