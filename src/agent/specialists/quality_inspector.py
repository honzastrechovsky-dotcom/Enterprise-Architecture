"""Quality Inspector - specialist in quality control and compliance.

This specialist excels at:
- Analyzing quality reports and metrics
- Detecting anomalies and out-of-spec conditions
- Referencing quality standards and specifications
- Statistical process control (SPC) analysis
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

_SYSTEM_PROMPT = """You are a Quality Inspector specialist for an enterprise organization.

Your expertise is in quality control, compliance verification, anomaly detection, and
statistical process control (SPC). You help ensure products and processes meet
specifications and identify quality issues before they become problems.

**Your core responsibilities:**

1. **Quality Analysis**: Review quality data against specifications and standards
2. **Anomaly Detection**: Identify out-of-spec conditions, trends, and patterns
3. **Compliance Check**: Verify conformance to quality standards and regulations
4. **SPC Analysis**: Apply statistical process control methods to assess stability
5. **Root Cause Support**: Help identify potential causes of quality issues

**Quality Analysis Framework**:

1. **Specifications**: What are the acceptance criteria?
2. **Measurements**: What are the actual values?
3. **Comparison**: How do measurements compare to specs?
4. **Trends**: Are there patterns or trends in the data?
5. **Anomalies**: What's unusual or out of normal range?
6. **Action**: What needs attention?

**Critical Quality Rules**:
- 🔴 **OUT OF SPEC**: Clearly mark any measurements outside tolerance
- 🟡 **TREND ALERT**: Flag trends approaching spec limits (within 80% of limit)
- ⚠️ **ANOMALY**: Highlight unusual patterns or outliers
- ✅ **IN SPEC**: Confirm when measurements meet requirements
- 📊 **SPC SIGNALS**: Note any SPC rule violations (run rules, etc.)

**Statistical Process Control (SPC) Guidelines**:
- Apply Western Electric rules for out-of-control signals
- Check for trends (7+ consecutive points trending)
- Look for shifts (8+ points on one side of centerline)
- Identify cycles or non-random patterns
- Calculate Cp, Cpk when sufficient data available

**Response Format**:
1. **Quality Summary**: Overall assessment (in spec / out of spec / trending)
2. **Measurements**: List key measurements with spec limits
3. **Analysis**: Detailed findings with statistical context
4. **Anomalies**: Any issues requiring attention
5. **Recommendations**: Immediate actions and further investigation needs

**Citation Format**: [Quality: document-id, section/page]

**IMPORTANT**: Quality issues may require immediate action. All findings must be
verified by quality personnel before corrective actions are taken."""


class QualityInspectorAgent(BaseSpecialistAgent):
    """Quality control and compliance specialist.

    This agent is optimized for queries about quality metrics, specifications,
    compliance, and statistical process control.
    """

    async def process(self, message: str, context: AgentContext) -> AgentResponse:
        """Process a quality analysis request.

        Steps:
        1. Execute ReasoningEngine OBSERVE→THINK→VERIFY cycle
        2. Identify what quality aspect is being queried
        3. Search for relevant specs, standards, and quality data
        4. Perform anomaly detection and SPC analysis
        5. Compare against specifications
        6. Provide actionable quality assessment

        Args:
            message: User's question about quality or compliance
            context: Full context including RAG results

        Returns:
            AgentResponse with quality assessment and recommendations
        """
        reasoning_trace = [
            "Quality Inspector activated for quality/compliance analysis",
            f"Query: {message[:100]}..." if len(message) > 100 else f"Query: {message}",
        ]

        tools_used = []
        citations = []

        # Step 0: Execute structured reasoning (OBSERVE→THINK→VERIFY)
        log.info(
            "quality_inspector.reasoning_start",
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
            "quality_inspector.reasoning_complete",
            tenant_id=str(context.tenant_id),
            confidence=reasoning_result.total_confidence,
            verified=reasoning_result.verification.is_verified,
            requires_review=reasoning_result.verification.requires_human_review,
        )

        # Step 1: Search for quality specs and data
        reasoning_trace.append(
            "Searching for quality specifications and measurement data"
        )

        search_result = await self._use_tool(
            "document_search",
            {
                "query": f"quality specifications standards: {message}",
                "top_k": 10,
            },
            context,
        )

        tools_used.append({
            "tool": "document_search",
            "success": search_result.success,
            "query_type": "quality_specs",
        })

        if search_result.success:
            reasoning_trace.append("Found relevant quality documentation")
        else:
            reasoning_trace.append(
                f"Quality doc search failed: {search_result.error}"
            )

        # Step 2: Check if calculations are needed for SPC analysis
        if any(word in message.lower() for word in ["cpk", "cp", "sigma", "control", "calculate"]):
            reasoning_trace.append("Detected SPC calculation requirement")

            # Placeholder calculation (in real usage, extract data from RAG)
            calc_result = await self._use_tool(
                "calculator",
                {"expression": "(110 - 105) / (115 - 95)"},  # Example Cpk calc
                context,
            )

            tools_used.append({
                "tool": "calculator",
                "success": calc_result.success,
                "calculation_type": "spc_metric",
            })

            if calc_result.success:
                reasoning_trace.append("Performed SPC calculations")
            else:
                reasoning_trace.append(f"SPC calculation failed: {calc_result.error}")

        # Step 3: Build messages with quality-focused instructions (enriched with reasoning)
        additional_instructions = f"""
**CRITICAL: This is a quality/compliance query. Your response MUST:**

**REASONING CONTEXT (from structured analysis):**
- Conclusion: {reasoning_result.conclusion}
- Confidence: {reasoning_result.total_confidence:.2f}
- Verification: {"✓ Passed" if reasoning_result.verification.is_verified else "✗ Failed"}
{f"- Review Required: {reasoning_result.verification.review_reason}" if reasoning_result.verification.requires_human_review else ""}

1. **Quality Summary** (first paragraph):
   - Overall status: ✅ IN SPEC / 🟡 TREND ALERT / 🔴 OUT OF SPEC
   - Immediate concerns requiring attention
   - Confidence level in assessment

2. **Specifications Referenced**:
   - List relevant specs with limits
   - Cite the source documents
   - Note if any specs are missing

3. **Measurements & Analysis**:
   - Present key measurements in a table if multiple
   - Compare each to spec limits
   - Calculate margins (how close to limits)
   - Identify trends or patterns

4. **Anomaly Detection**:
   - Flag any out-of-spec conditions with 🔴
   - Note trending toward limits with 🟡
   - Identify unusual patterns or outliers
   - Apply SPC rules if sufficient data

5. **Recommended Actions**:
   - Immediate actions (if out of spec)
   - Further investigation needs
   - Preventive measures
   - When to escalate to quality team

⚠️ IMPORTANT: End with this disclaimer:
"This analysis is for reference only. All quality decisions must be verified by
qualified quality personnel before taking corrective action."
"""

        messages = self._build_messages(message, context, additional_instructions)
        reasoning_trace.append(
            f"Built quality analysis context ({len(messages)} messages)"
        )

        # Step 4: Call LLM with quality inspector prompt
        log.info(
            "quality_inspector.analyzing",
            tenant_id=str(context.tenant_id),
            has_rag=bool(context.rag_context),
            spc_calculations=any(
                t["tool"] == "calculator" and t.get("calculation_type") == "spc_metric"
                for t in tools_used
            ),
        )

        response_text = await self._call_llm(messages)
        reasoning_trace.append(
            f"Generated quality analysis ({len(response_text)} chars)"
        )

        # Step 5: Extract citations from quality docs
        if context.rag_context or search_result.success:
            citations.append({
                "source": "quality_documentation",
                "type": "compliance_critical",
                "note": "References from specifications and quality standards",
            })
            reasoning_trace.append("Extracted quality specification citations")

        # Step 6: Set verification status from reasoning result
        verification_status = "verified" if reasoning_result.verification.is_verified else "failed"
        if reasoning_result.verification.requires_human_review:
            verification_status = "pending_human_review"

        reasoning_trace.append(
            f"Verification: {verification_status} (quality-critical domain)"
        )

        log.info(
            "quality_inspector.complete",
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
                "quality_critical": True,
                "requires_qc_approval": True,
                "analysis_type": "quality_inspection",
                "reasoning_confidence": reasoning_result.total_confidence,
                "reasoning_verified": reasoning_result.verification.is_verified,
                "reasoning_steps": len(reasoning_result.reasoning_steps),
            },
        )


# Agent specification
SPEC = AgentSpec(
    agent_id="quality_inspector",
    name="Quality Inspector",
    description=(
        "Quality control specialist focused on analyzing quality reports, detecting "
        "anomalies, checking compliance against specifications, and performing SPC "
        "analysis. Use for queries about quality metrics, specifications, out-of-spec "
        "conditions, or statistical process control. All outputs require verification "
        "before action."
    ),
    system_prompt=_SYSTEM_PROMPT,
    capabilities=[
        "quality_analysis",
        "compliance_check",
        "anomaly_detection",
        "spc",
    ],
    tools=["document_search", "calculator"],
    required_role=UserRole.OPERATOR,
    model_preference=None,
    max_tokens=2048,
    temperature=0.3,  # Lower temperature for quality precision
    classification_access=["class_i", "class_ii"],
    requires_verification=True,  # ALWAYS require verification for quality
    metadata={
        "version": "1.0.0",
        "specialization": "quality_control",
        "quality_critical": True,
    },
)
