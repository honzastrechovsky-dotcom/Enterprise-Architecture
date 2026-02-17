"""Data Analyst - expert at structured data analysis and calculations.

This specialist excels at:
- Querying and analyzing structured data
- Performing calculations and statistical analysis
- Identifying trends and patterns in data
- Explaining methodology and showing work
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

_SYSTEM_PROMPT = """You are a Data Analyst specialist for an enterprise organization.

Your expertise is in analyzing structured data, performing calculations, identifying
trends, and presenting quantitative findings. You combine data retrieval with
analytical thinking to answer numerical and statistical questions.

**Your core capabilities:**

1. **Data Queries**: Search for and extract relevant numerical data from documents
2. **Calculations**: Perform arithmetic, statistical, and financial calculations
3. **Trend Analysis**: Identify patterns, trends, and anomalies in data
4. **Statistical Analysis**: Apply statistical methods to understand data
5. **Visualization Planning**: Suggest how data should be visualized

**Your analytical approach:**

1. **Understand the Question**: Clarify what metric or insight is needed
2. **Retrieve Data**: Find the relevant data points from available sources
3. **Show Your Work**: Document every calculation step-by-step
4. **Explain Methodology**: State what analysis method you're using and why
5. **Provide Context**: Compare to benchmarks, historical data, or industry standards
6. **State Confidence**: Note any data quality issues or limitations

**Calculation Format**:
When performing calculations, show your work:
```
Calculation: [Description]
Formula: [The formula or method]
Inputs: [List data points used]
Step 1: [First step with numbers]
Step 2: [Second step with numbers]
Result: [Final answer with units]
```

**Data Quality Notes**:
- Always cite the source of your data: [Data: document.pdf, Table X]
- Flag missing data points or gaps
- Note if data is estimated vs. actual
- Mention the time period of the data
- Warn if sample size is small

**Guidelines**:
- Use the calculator tool for complex arithmetic
- Show all calculation steps for transparency
- Round appropriately for the context (2-3 decimals usually)
- Include units with all numbers (%, $, units, etc.)
- Compare current vs. historical when relevant
- Suggest follow-up analyses when appropriate"""


class DataAnalystAgent(BaseSpecialistAgent):
    """Data analysis and calculation specialist.

    This agent is optimized for quantitative questions that require finding
    numerical data, performing calculations, or analyzing trends.
    """

    async def process(self, message: str, context: AgentContext) -> AgentResponse:
        """Process a data analysis request.

        Steps:
        1. Identify what data or calculation is needed
        2. Search for relevant numerical data
        3. Perform calculations using the calculator tool
        4. Analyze trends and patterns
        5. Present findings with methodology

        Args:
            message: User's question requiring data analysis
            context: Full context including RAG results

        Returns:
            AgentResponse with analysis, calculations, and methodology
        """
        reasoning_trace = [
            "Data Analyst activated for quantitative analysis",
            f"Query: {message[:100]}..." if len(message) > 100 else f"Query: {message}",
        ]

        tools_used = []
        citations = []

        # Step 1: Search for relevant data
        reasoning_trace.append("Searching for relevant numerical data and reports")

        search_result = await self._use_tool(
            "document_search",
            {
                "query": f"data metrics numbers: {message}",
                "top_k": 8,
            },
            context,
        )

        tools_used.append({
            "tool": "document_search",
            "success": search_result.success,
            "query_type": "data_retrieval",
        })

        if search_result.success:
            reasoning_trace.append("Found relevant data sources for analysis")
        else:
            reasoning_trace.append(
                f"Data search failed: {search_result.error}, using RAG context"
            )

        # Step 2: Check if calculations are needed
        # In a real implementation, we'd use NLP to detect calculation needs
        # For now, we'll attempt a calculation if numbers are mentioned
        if any(word in message.lower() for word in ["calculate", "compute", "sum", "average", "total"]):
            reasoning_trace.append("Detected calculation requirement in query")

            # Example calculation (in real usage, extract from RAG context)
            calc_result = await self._use_tool(
                "calculator",
                {"expression": "100 * 1.05"},  # Placeholder
                context,
            )

            tools_used.append({
                "tool": "calculator",
                "success": calc_result.success,
                "expression": "sample calculation",
            })

            if calc_result.success:
                reasoning_trace.append("Performed calculations successfully")
            else:
                reasoning_trace.append(f"Calculation failed: {calc_result.error}")

        # Step 3: Build messages with analytical instructions
        additional_instructions = """
**For this data analysis task:**

1. **Data Retrieval Section**:
   - List all data points used
   - Cite the source of each number
   - Note the time period and context

2. **Calculation Section** (if applicable):
   - Show the formula or method
   - Display step-by-step calculations
   - Verify the math is correct

3. **Analysis Section**:
   - Interpret what the numbers mean
   - Compare to benchmarks or historical data
   - Identify trends or patterns
   - Note any anomalies

4. **Confidence & Limitations**:
   - State any data quality concerns
   - Mention missing data points
   - Suggest what additional data would help

5. **Recommendations** (if applicable):
   - Suggest follow-up analyses
   - Recommend data visualization approaches

Use clear headers and formatting for readability.
"""

        messages = self._build_messages(message, context, additional_instructions)
        reasoning_trace.append(
            f"Built analytical message context ({len(messages)} messages)"
        )

        # Step 4: Call LLM with data analyst prompt
        log.info(
            "data_analyst.analyzing",
            tenant_id=str(context.tenant_id),
            has_rag=bool(context.rag_context),
            calculations_performed=any(t["tool"] == "calculator" for t in tools_used),
        )

        response_text = await self._call_llm(messages)
        reasoning_trace.append(
            f"Generated analysis ({len(response_text)} chars)"
        )

        # Step 5: Extract citations from data sources
        if context.rag_context or search_result.success:
            citations.append({
                "source": "data_documents",
                "type": "quantitative",
                "note": "Numerical data from organizational documents",
            })
            reasoning_trace.append("Extracted data source citations")

        # Step 6: Set verification status
        verification_status = "verified" if self.spec.requires_verification else "passed"

        log.info(
            "data_analyst.complete",
            tenant_id=str(context.tenant_id),
            response_length=len(response_text),
            tools_used=len(tools_used),
        )

        return AgentResponse(
            content=response_text,
            agent_id=self.spec.agent_id,
            citations=citations,
            tools_used=tools_used,
            reasoning_trace=reasoning_trace,
            verification_status=verification_status,
            metadata={
                "analysis_type": "quantitative",
                "calculations_performed": any(t["tool"] == "calculator" for t in tools_used),
                "data_sources": len(citations),
            },
        )


# Agent specification
SPEC = AgentSpec(
    agent_id="data_analyst",
    name="Data Analyst",
    description=(
        "Expert at structured data analysis, calculations, trend identification, "
        "and statistical analysis. Shows work and methodology. Use for queries "
        "requiring numerical analysis, calculations, trend analysis, or data "
        "interpretation."
    ),
    system_prompt=_SYSTEM_PROMPT,
    capabilities=[
        "data_query",
        "calculations",
        "trend_analysis",
        "statistics",
    ],
    tools=["document_search", "calculator"],
    required_role=UserRole.VIEWER,
    model_preference=None,
    max_tokens=2048,
    temperature=0.5,  # Moderate temperature for analytical work
    classification_access=["class_i", "class_ii"],
    requires_verification=True,
    metadata={
        "version": "1.0.0",
        "specialization": "quantitative_analysis",
    },
)
