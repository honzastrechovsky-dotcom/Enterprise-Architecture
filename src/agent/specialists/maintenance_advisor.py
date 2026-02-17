"""Maintenance Advisor - specialist in equipment maintenance and P&ID interpretation.

This specialist excels at:
- Equipment maintenance planning and scheduling
- Interpreting P&IDs (Piping and Instrumentation Diagrams)
- Equipment troubleshooting and diagnostics
- Drafting work orders and maintenance procedures
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

_SYSTEM_PROMPT = """You are a Maintenance Advisor specialist for an enterprise organization.

Your expertise is in equipment maintenance, P&ID interpretation, troubleshooting,
and maintenance planning. You help maintenance teams keep equipment running safely
and efficiently.

**Your core capabilities:**

1. **Maintenance Planning**: Recommend maintenance schedules and preventive actions
2. **P&ID Interpretation**: Read and explain Piping & Instrumentation Diagrams
3. **Equipment Knowledge**: Reference equipment manuals, specs, and history
4. **Troubleshooting**: Help diagnose equipment issues and recommend fixes
5. **Work Order Drafting**: Create clear maintenance work orders with safety notes

**Maintenance Analysis Framework**:

1. **Equipment Identification**: What equipment or system is involved?
2. **Current Status**: What's the issue or maintenance need?
3. **Safety Considerations**: What hazards or lockout/tagout is required?
4. **Diagnostic Steps**: How to investigate or confirm the issue?
5. **Corrective Actions**: What work needs to be done?
6. **Parts/Resources**: What materials, tools, or skills are needed?

**Safety-Critical Rules**:
- 🔒 **LOCKOUT/TAGOUT**: Always specify LOTO requirements
- ⚠️ **HAZARDS**: Identify electrical, mechanical, chemical, or other hazards
- 👷 **PERMISSIONS**: Note if work requires certified technicians
- 🛑 **SHUTDOWN REQUIRED**: Specify if equipment must be offline
- 📋 **PERMITS**: List required work permits (hot work, confined space, etc.)

**P&ID Interpretation Guidelines**:
- Identify components by tag numbers (e.g., V-101, P-203)
- Explain flow paths and control logic
- Reference instrument types and their functions
- Note critical control points and safety interlocks
- Describe equipment relationships and dependencies

**Work Order Format**:
```
**Equipment**: [Tag number and description]
**Priority**: [Routine / Preventive / Corrective / Emergency]
**Issue**: [Problem description]
**Safety Requirements**:
  - LOTO: [Steps]
  - PPE: [Required equipment]
  - Permits: [Required permits]
**Work Steps**:
  1. [Clear, actionable steps]
  2. [...]
**Parts Needed**: [List with part numbers if known]
**Estimated Time**: [Duration]
**Skills Required**: [Technician level/certifications]
```

**Citation Format**: [Manual: equipment-id, section] or [P&ID: drawing-number]

**CRITICAL**: All maintenance recommendations are advisory only. Maintenance work
must be performed by qualified technicians following safety procedures and supervisor
approval."""


class MaintenanceAdvisorAgent(BaseSpecialistAgent):
    """Equipment maintenance and P&ID specialist.

    This agent is optimized for queries about equipment maintenance,
    troubleshooting, P&ID interpretation, and work planning.
    """

    async def process(self, message: str, context: AgentContext) -> AgentResponse:
        """Process a maintenance-related query.

        Steps:
        1. Execute ReasoningEngine OBSERVE→THINK→VERIFY cycle
        2. Identify the equipment or system being asked about
        3. Search for relevant manuals, P&IDs, and maintenance records
        4. Analyze the maintenance need or issue
        5. Recommend maintenance actions with safety considerations
        6. Draft work order if appropriate

        Args:
            message: User's question about maintenance or equipment
            context: Full context including RAG results

        Returns:
            AgentResponse with maintenance recommendations and safety notes
        """
        reasoning_trace = [
            "Maintenance Advisor activated for equipment/maintenance query",
            f"Query: {message[:100]}..." if len(message) > 100 else f"Query: {message}",
        ]

        tools_used = []
        citations = []

        # Step 0: Execute structured reasoning (OBSERVE→THINK→VERIFY)
        log.info(
            "maintenance_advisor.reasoning_start",
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
            "maintenance_advisor.reasoning_complete",
            tenant_id=str(context.tenant_id),
            confidence=reasoning_result.total_confidence,
            verified=reasoning_result.verification.is_verified,
            requires_review=reasoning_result.verification.requires_human_review,
        )

        # Step 1: Search for equipment documentation
        reasoning_trace.append(
            "Searching for equipment manuals, P&IDs, and maintenance records"
        )

        search_result = await self._use_tool(
            "document_search",
            {
                "query": f"equipment maintenance manual P&ID: {message}",
                "top_k": 10,
            },
            context,
        )

        tools_used.append({
            "tool": "document_search",
            "success": search_result.success,
            "query_type": "equipment_documentation",
        })

        if search_result.success:
            reasoning_trace.append("Found relevant equipment documentation")
        else:
            reasoning_trace.append(
                f"Equipment doc search failed: {search_result.error}"
            )

        # Step 2: Determine if this is a P&ID interpretation query
        is_pid_query = any(
            keyword in message.lower()
            for keyword in ["p&id", "pid", "piping", "instrumentation", "diagram", "tag"]
        )

        if is_pid_query:
            reasoning_trace.append("Detected P&ID interpretation query")

        # Step 3: Determine if this is a work order request
        is_work_order = any(
            keyword in message.lower()
            for keyword in ["work order", "maintenance request", "schedule", "plan"]
        )

        if is_work_order:
            reasoning_trace.append("Detected work order creation request")

        # Step 4: Build messages with maintenance-focused instructions (enriched with reasoning)
        additional_instructions = f"""
**CRITICAL: This is a maintenance/equipment query. Your response MUST:**

**REASONING CONTEXT (from structured analysis):**
- Conclusion: {reasoning_result.conclusion}
- Confidence: {reasoning_result.total_confidence:.2f}
- Verification: {"✓ Passed" if reasoning_result.verification.is_verified else "✗ Failed"}
{f"- Review Required: {reasoning_result.verification.review_reason}" if reasoning_result.verification.requires_human_review else ""}

1. **Equipment Identification**:
   - Equipment tag or ID
   - Equipment type and function
   - Location if known

2. **Safety Assessment** (ALWAYS INCLUDE):
   - 🔒 Lockout/Tagout requirements
   - ⚠️ Hazards (electrical, mechanical, chemical, pressure, etc.)
   - Required PPE
   - Required work permits
   - 👷 Required technician qualifications

3. **Analysis/Recommendations**:
   - For P&ID queries: Explain components, flow, and control logic
   - For troubleshooting: Diagnostic steps and likely causes
   - For maintenance planning: Recommended actions and schedule
   - For work orders: Detailed work steps

4. **Resources Needed**:
   - Parts with part numbers (if known)
   - Special tools required
   - Estimated time
   - Number of technicians needed

5. **References**:
   - Cite equipment manuals
   - Reference P&ID drawings
   - Note relevant maintenance history

⚠️ IMPORTANT: End with this disclaimer:
"This is advisory guidance only. All maintenance work must be performed by qualified
technicians following approved safety procedures and with supervisor authorization.
Human verification required before executing any maintenance recommendations."
"""

        messages = self._build_messages(message, context, additional_instructions)
        reasoning_trace.append(
            f"Built maintenance-focused context ({len(messages)} messages)"
        )

        # Step 5: Call LLM with maintenance advisor prompt
        log.info(
            "maintenance_advisor.analyzing",
            tenant_id=str(context.tenant_id),
            has_rag=bool(context.rag_context),
            is_pid_query=is_pid_query,
            is_work_order=is_work_order,
        )

        response_text = await self._call_llm(messages)
        reasoning_trace.append(
            f"Generated maintenance guidance ({len(response_text)} chars)"
        )

        # Step 6: Extract citations from equipment docs
        if context.rag_context or search_result.success:
            citation_type = "pid_reference" if is_pid_query else "maintenance_documentation"
            citations.append({
                "source": "equipment_documentation",
                "type": citation_type,
                "note": "References from equipment manuals and P&IDs",
            })
            reasoning_trace.append("Extracted equipment documentation citations")

        # Step 7: Set verification status from reasoning result
        verification_status = "verified" if reasoning_result.verification.is_verified else "failed"
        if reasoning_result.verification.requires_human_review:
            verification_status = "pending_human_review"

        reasoning_trace.append(
            f"Verification: {verification_status} (safety-critical maintenance domain)"
        )

        log.info(
            "maintenance_advisor.complete",
            tenant_id=str(context.tenant_id),
            response_length=len(response_text),
            verification_required=True,
            is_work_order=is_work_order,
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
                "query_type": "pid_interpretation" if is_pid_query else "maintenance_planning",
                "work_order_generated": is_work_order,
                "reasoning_confidence": reasoning_result.total_confidence,
                "reasoning_verified": reasoning_result.verification.is_verified,
                "reasoning_steps": len(reasoning_result.reasoning_steps),
            },
        )


# Agent specification
SPEC = AgentSpec(
    agent_id="maintenance_advisor",
    name="Maintenance Advisor",
    description=(
        "Equipment maintenance specialist focused on maintenance planning, P&ID "
        "interpretation, equipment troubleshooting, and work order drafting. "
        "Use for queries about equipment maintenance, P&ID diagrams, troubleshooting, "
        "or work planning. All recommendations are safety-critical and require human "
        "approval before execution."
    ),
    system_prompt=_SYSTEM_PROMPT,
    capabilities=[
        "maintenance_planning",
        "pid_interpretation",
        "equipment_query",
        "work_order",
    ],
    tools=["document_search"],
    required_role=UserRole.OPERATOR,
    model_preference=None,
    max_tokens=2048,
    temperature=0.4,  # Moderate-low for maintenance accuracy
    classification_access=["class_i", "class_ii"],
    requires_verification=True,  # ALWAYS require verification for maintenance
    metadata={
        "version": "1.0.0",
        "specialization": "equipment_maintenance",
        "safety_critical": True,
    },
)
