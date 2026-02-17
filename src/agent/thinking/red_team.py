"""Red Team adversarial analysis for agent responses.

This module implements adversarial stress-testing of agent outputs before they
reach the user. It runs parallel checks to catch:

1. Factual grounding issues (contradictions with source docs)
2. Safety omissions (missing warnings for hazardous operations)
3. Confidence calibration problems (overconfidence without evidence)
4. Classification leakage (response exceeds user's clearance level)

The red team uses 5 LLM calls:
- 4 parallel checks (one per category)
- 1 aggregation call to determine overall severity

CRITICAL findings block the response and require human review.

Usage:
    red_team = RedTeam(llm_client)
    result = await red_team.analyze(
        response="Agent's draft response...",
        sources=["doc1.txt: content", "doc2.pdf: content"],
        clearance="class_ii",
    )

    if result.requires_human_review:
        # Block response, escalate to human
        ...
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from enum import Enum

import structlog

from src.agent.llm import LLMClient

log = structlog.get_logger(__name__)


class Severity(str, Enum):
    """Severity levels for adversarial findings."""

    CRITICAL = "critical"  # Blocks response, requires human review
    HIGH = "high"  # Major concern, adjust response
    MEDIUM = "medium"  # Minor concern, note in logs
    LOW = "low"  # Informational, no action needed


@dataclass
class AdversarialFinding:
    """A single finding from red team analysis.

    Attributes:
        category: Which check found this (factual, safety, confidence, classification)
        severity: How serious this issue is
        description: Human-readable description of the issue
        evidence: Specific quotes or examples demonstrating the issue
        recommendation: How to address this issue
    """

    category: str
    severity: Severity
    description: str
    evidence: list[str]
    recommendation: str


@dataclass
class RedTeamResult:
    """Complete result from red team adversarial analysis.

    Attributes:
        findings: All issues found, sorted by severity
        overall_severity: Worst severity across all findings
        requires_human_review: True if any CRITICAL findings exist
        overall_confidence: Confidence in the response after red team review (0.0-1.0)
        review_reason: Explanation if human review is required
    """

    findings: list[AdversarialFinding]
    overall_severity: Severity
    requires_human_review: bool
    overall_confidence: float
    review_reason: str | None


class RedTeam:
    """Adversarial analysis engine for stress-testing agent responses.

    Uses parallel LLM calls to check for factual grounding, safety omissions,
    confidence calibration, and classification leakage. Aggregates findings
    into a result that determines if the response should be blocked.

    Example:
        red_team = RedTeam(llm_client)
        result = await red_team.analyze(
            response=draft_response,
            sources=rag_docs,
            clearance=user.clearance,
        )

        if result.requires_human_review:
            log.critical("red_team.blocked", reason=result.review_reason)
            # Escalate to human
    """

    def __init__(self, llm_client: LLMClient) -> None:
        """Initialize red team analyzer.

        Args:
            llm_client: LLM client for analysis calls (uses temperature=0.2)
        """
        self._llm = llm_client

    async def analyze(
        self,
        *,
        response: str,
        sources: list[str],
        clearance: str,
        query: str | None = None,
    ) -> RedTeamResult:
        """Run adversarial analysis on an agent response.

        This orchestrates 4 parallel checks and 1 aggregation:
        1. Factual grounding check
        2. Safety omissions check
        3. Confidence calibration check
        4. Classification leakage check
        5. Aggregate findings and determine severity

        Args:
            response: The agent's draft response to analyze
            sources: Source documents cited in the response
            clearance: User's classification clearance level
            query: Original user query (optional, for context)

        Returns:
            RedTeamResult with all findings and escalation decision
        """
        log.info(
            "red_team.starting",
            response_length=len(response),
            source_count=len(sources),
            clearance=clearance,
        )

        # Run all 4 checks in parallel
        results = await asyncio.gather(
            self._check_factual_grounding(response, sources),
            self._check_safety_omissions(response, query or ""),
            self._check_confidence_calibration(response, sources),
            self._check_classification_leakage(response, clearance),
            return_exceptions=True,
        )

        # Flatten findings from all checks
        all_findings: list[AdversarialFinding] = []
        for result in results:
            if isinstance(result, list):
                all_findings.extend(result)
            elif isinstance(result, Exception):
                # Log error but don't fail entire analysis
                log.error("red_team.check_failed", error=str(result))
                # Add a fallback finding
                all_findings.append(
                    AdversarialFinding(
                        category="system_error",
                        severity=Severity.HIGH,
                        description=f"Red team check failed: {str(result)[:100]}",
                        evidence=[],
                        recommendation="Retry analysis or escalate to human review",
                    )
                )

        # Aggregate findings to determine overall result
        final_result = await self._aggregate_findings(all_findings, response)

        log.info(
            "red_team.complete",
            finding_count=len(all_findings),
            overall_severity=final_result.overall_severity.value,
            requires_review=final_result.requires_human_review,
            confidence=f"{final_result.overall_confidence:.2f}",
        )

        return final_result

    async def _check_factual_grounding(
        self, response: str, sources: list[str]
    ) -> list[AdversarialFinding]:
        """Check if response contradicts or misrepresents source documents.

        Args:
            response: Agent's draft response
            sources: Source documents cited

        Returns:
            List of factual grounding issues found
        """
        sources_text = "\n\n".join(sources[:5]) if sources else "No sources provided"

        prompt = f"""You are a fact-checking adversarial reviewer. Check if the response
contradicts or misrepresents the source documents.

Response to check:
{response}

Source documents:
{sources_text[:3000]}

Identify factual grounding issues:
1. Statements that contradict sources
2. Claims not supported by sources
3. Misrepresentation of source content
4. Hallucinated facts not in sources

Respond in JSON format:
{{
    "findings": [
        {{
            "severity": "critical|high|medium|low",
            "description": "Description of the issue",
            "evidence": ["quote from response", "relevant source quote"],
            "recommendation": "How to fix this"
        }}
    ]
}}

If no issues found, return {{"findings": []}}.
Respond ONLY with valid JSON, no additional text."""

        messages = [
            {
                "role": "system",
                "content": "You are a fact-checking assistant. Always respond with valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            llm_response = await self._llm.complete(
                messages=messages,
                temperature=0.2,  # Low temperature for consistent analysis
                max_tokens=1024,
            )
            response_text = self._llm.extract_text(llm_response)

            parsed = json.loads(response_text)
            findings_data = parsed.get("findings", [])

            return [
                AdversarialFinding(
                    category="factual_grounding",
                    severity=Severity(finding.get("severity", "medium")),
                    description=finding.get("description", "Unknown issue"),
                    evidence=finding.get("evidence", []),
                    recommendation=finding.get("recommendation", "Review and correct"),
                )
                for finding in findings_data
            ]

        except json.JSONDecodeError:
            log.warning("red_team.factual_check_json_failed")
            return [
                AdversarialFinding(
                    category="factual_grounding",
                    severity=Severity.MEDIUM,
                    description="Unable to parse fact-check results",
                    evidence=[],
                    recommendation="Retry fact-checking or escalate to human",
                )
            ]
        except Exception as exc:
            log.error("red_team.factual_check_failed", error=str(exc))
            raise

    async def _check_safety_omissions(
        self, response: str, query: str
    ) -> list[AdversarialFinding]:
        """Check if response omits critical safety warnings.

        Args:
            response: Agent's draft response
            query: Original user query

        Returns:
            List of safety omission issues found
        """
        prompt = f"""You are a safety-focused adversarial reviewer. Check if the response
omits critical safety warnings for potentially hazardous operations.

User query:
{query}

Response to check:
{response}

Identify safety omissions:
1. Hazardous operations without warnings
2. Missing prerequisites or safety checks
3. Dangerous shortcuts or assumptions
4. Lack of failure mode discussion

Respond in JSON format:
{{
    "findings": [
        {{
            "severity": "critical|high|medium|low",
            "description": "Description of the omission",
            "evidence": ["quote showing omission"],
            "recommendation": "What safety warning to add"
        }}
    ]
}}

If no issues found, return {{"findings": []}}.
Respond ONLY with valid JSON, no additional text."""

        messages = [
            {
                "role": "system",
                "content": "You are a safety analysis assistant. Always respond with valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            llm_response = await self._llm.complete(
                messages=messages,
                temperature=0.2,
                max_tokens=1024,
            )
            response_text = self._llm.extract_text(llm_response)

            parsed = json.loads(response_text)
            findings_data = parsed.get("findings", [])

            return [
                AdversarialFinding(
                    category="safety_omissions",
                    severity=Severity(finding.get("severity", "medium")),
                    description=finding.get("description", "Unknown issue"),
                    evidence=finding.get("evidence", []),
                    recommendation=finding.get("recommendation", "Add safety warning"),
                )
                for finding in findings_data
            ]

        except json.JSONDecodeError:
            log.warning("red_team.safety_check_json_failed")
            return [
                AdversarialFinding(
                    category="safety_omissions",
                    severity=Severity.MEDIUM,
                    description="Unable to parse safety check results",
                    evidence=[],
                    recommendation="Retry safety check or escalate to human",
                )
            ]
        except Exception as exc:
            log.error("red_team.safety_check_failed", error=str(exc))
            raise

    async def _check_confidence_calibration(
        self, response: str, sources: list[str]
    ) -> list[AdversarialFinding]:
        """Check if response confidence is justified by available evidence.

        Args:
            response: Agent's draft response
            sources: Source documents

        Returns:
            List of confidence calibration issues found
        """
        sources_text = "\n\n".join(sources[:5]) if sources else "No sources provided"

        prompt = f"""You are a confidence calibration adversarial reviewer. Check if the
response's confidence level is justified by the available evidence.

Response to check:
{response}

Available evidence:
{sources_text[:3000]}

Identify confidence calibration issues:
1. Definitive statements without strong evidence
2. Missing uncertainty acknowledgment
3. Overconfident claims on ambiguous data
4. Lack of confidence qualifiers where needed

Respond in JSON format:
{{
    "findings": [
        {{
            "severity": "critical|high|medium|low",
            "description": "Description of the issue",
            "evidence": ["overconfident quote", "weak evidence"],
            "recommendation": "How to calibrate confidence"
        }}
    ]
}}

If no issues found, return {{"findings": []}}.
Respond ONLY with valid JSON, no additional text."""

        messages = [
            {
                "role": "system",
                "content": "You are a confidence analysis assistant. Always respond with valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            llm_response = await self._llm.complete(
                messages=messages,
                temperature=0.2,
                max_tokens=1024,
            )
            response_text = self._llm.extract_text(llm_response)

            parsed = json.loads(response_text)
            findings_data = parsed.get("findings", [])

            return [
                AdversarialFinding(
                    category="confidence_calibration",
                    severity=Severity(finding.get("severity", "medium")),
                    description=finding.get("description", "Unknown issue"),
                    evidence=finding.get("evidence", []),
                    recommendation=finding.get(
                        "recommendation", "Add uncertainty qualifiers"
                    ),
                )
                for finding in findings_data
            ]

        except json.JSONDecodeError:
            log.warning("red_team.confidence_check_json_failed")
            return [
                AdversarialFinding(
                    category="confidence_calibration",
                    severity=Severity.MEDIUM,
                    description="Unable to parse confidence check results",
                    evidence=[],
                    recommendation="Retry confidence check or escalate to human",
                )
            ]
        except Exception as exc:
            log.error("red_team.confidence_check_failed", error=str(exc))
            raise

    async def _check_classification_leakage(
        self, response: str, clearance: str
    ) -> list[AdversarialFinding]:
        """Check if response contains information above user's clearance level.

        Args:
            response: Agent's draft response
            clearance: User's classification clearance level

        Returns:
            List of classification leakage issues found
        """
        prompt = f"""You are a classification security adversarial reviewer. Check if the
response contains information above the user's clearance level.

User clearance: {clearance}

Response to check:
{response}

Identify classification leakage:
1. Information marked above user's clearance
2. Implied classified information
3. Detailed technical specs that should be restricted
4. Names, codes, or identifiers above clearance

Respond in JSON format:
{{
    "findings": [
        {{
            "severity": "critical|high|medium|low",
            "description": "Description of the leakage",
            "evidence": ["quote showing classified info"],
            "recommendation": "How to redact or rephrase"
        }}
    ]
}}

If no issues found, return {{"findings": []}}.
Respond ONLY with valid JSON, no additional text."""

        messages = [
            {
                "role": "system",
                "content": "You are a classification security assistant. Always respond with valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            llm_response = await self._llm.complete(
                messages=messages,
                temperature=0.2,
                max_tokens=1024,
            )
            response_text = self._llm.extract_text(llm_response)

            parsed = json.loads(response_text)
            findings_data = parsed.get("findings", [])

            return [
                AdversarialFinding(
                    category="classification_leakage",
                    severity=Severity(finding.get("severity", "medium")),
                    description=finding.get("description", "Unknown issue"),
                    evidence=finding.get("evidence", []),
                    recommendation=finding.get("recommendation", "Redact sensitive info"),
                )
                for finding in findings_data
            ]

        except json.JSONDecodeError:
            log.warning("red_team.classification_check_json_failed")
            return [
                AdversarialFinding(
                    category="classification_leakage",
                    severity=Severity.MEDIUM,
                    description="Unable to parse classification check results",
                    evidence=[],
                    recommendation="Retry classification check or escalate to human",
                )
            ]
        except Exception as exc:
            log.error("red_team.classification_check_failed", error=str(exc))
            raise

    async def _aggregate_findings(
        self, findings: list[AdversarialFinding], response: str
    ) -> RedTeamResult:
        """Aggregate findings and determine overall severity and action.

        Uses an LLM call to consider all findings holistically and determine:
        - Overall severity (worst finding's severity)
        - Whether human review is required
        - Adjusted confidence level
        - Review reason if escalation needed

        Args:
            findings: All findings from parallel checks
            response: Original response being analyzed

        Returns:
            Complete RedTeamResult
        """
        if not findings:
            # No findings = clean response
            return RedTeamResult(
                findings=[],
                overall_severity=Severity.LOW,
                requires_human_review=False,
                overall_confidence=1.0,
                review_reason=None,
            )

        # Determine worst severity
        severity_order = {
            Severity.CRITICAL: 4,
            Severity.HIGH: 3,
            Severity.MEDIUM: 2,
            Severity.LOW: 1,
        }
        overall_severity = max(findings, key=lambda f: severity_order[f.severity]).severity

        # CRITICAL findings always require human review
        critical_findings = [f for f in findings if f.severity == Severity.CRITICAL]
        if critical_findings:
            return RedTeamResult(
                findings=sorted(
                    findings, key=lambda f: severity_order[f.severity], reverse=True
                ),
                overall_severity=Severity.CRITICAL,
                requires_human_review=True,
                overall_confidence=0.2,  # Very low confidence if critical issues exist
                review_reason=f"CRITICAL issues found: {', '.join(f.category for f in critical_findings)}",
            )

        # For non-critical findings, use LLM to aggregate
        findings_summary = "\n".join(
            f"- [{f.severity.value.upper()}] {f.category}: {f.description}"
            for f in findings
        )

        prompt = f"""You are aggregating adversarial analysis findings. Given the findings,
determine if the response should be sent or requires human review.

Findings:
{findings_summary}

Response length: {len(response)} chars

Provide aggregation in JSON format:
{{
    "requires_human_review": true/false,
    "overall_confidence": 0.0-1.0,
    "review_reason": "Reason if review needed, or null"
}}

Guidelines:
- HIGH severity: Usually requires review
- Multiple MEDIUM: May require review
- Single MEDIUM or LOW: Usually safe to send

Respond ONLY with valid JSON, no additional text."""

        messages = [
            {
                "role": "system",
                "content": "You are an aggregation assistant. Always respond with valid JSON only.",
            },
            {"role": "user", "content": prompt},
        ]

        try:
            llm_response = await self._llm.complete(
                messages=messages,
                temperature=0.2,
                max_tokens=512,
            )
            response_text = self._llm.extract_text(llm_response)

            parsed = json.loads(response_text)

            return RedTeamResult(
                findings=sorted(
                    findings, key=lambda f: severity_order[f.severity], reverse=True
                ),
                overall_severity=overall_severity,
                requires_human_review=parsed.get("requires_human_review", False),
                overall_confidence=float(parsed.get("overall_confidence", 0.7)),
                review_reason=parsed.get("review_reason"),
            )

        except (json.JSONDecodeError, Exception) as exc:
            log.warning("red_team.aggregation_failed", error=str(exc))
            # Conservative fallback: require review if HIGH severity exists
            high_findings = [f for f in findings if f.severity == Severity.HIGH]
            return RedTeamResult(
                findings=sorted(
                    findings, key=lambda f: severity_order[f.severity], reverse=True
                ),
                overall_severity=overall_severity,
                requires_human_review=bool(high_findings),
                overall_confidence=0.6 if high_findings else 0.8,
                review_reason="HIGH severity findings detected (aggregation failed)"
                if high_findings
                else None,
            )
