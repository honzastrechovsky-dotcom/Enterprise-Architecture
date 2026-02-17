"""PII sanitization for AI prompts and responses.

Configurable per-tenant: redact, warn, or block when PII patterns are detected.
Patterns include employee IDs, email addresses, phone numbers, IP addresses,
and configurable custom patterns.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

import structlog

log = structlog.get_logger(__name__)


class PIIAction(StrEnum):
    """Action to take when PII is detected."""

    REDACT = "redact"  # Replace PII with placeholder
    WARN = "warn"  # Log warning, allow through
    BLOCK = "block"  # Reject request entirely


@dataclass
class PIIPattern:
    """A pattern for detecting PII in text."""

    name: str
    pattern: str  # Regex pattern
    replacement: str  # Replacement text for REDACT action
    action: PIIAction


@dataclass
class PIIFinding:
    """A detected PII match in text."""

    pattern_name: str
    match: str
    start: int
    end: int


@dataclass
class PIIScanResult:
    """Result of scanning text for PII."""

    has_pii: bool
    findings: list[PIIFinding]


@dataclass
class PIICheckResult:
    """Result of PII check with configured action applied."""

    allowed: bool
    sanitized_text: str | None
    action_taken: PIIAction
    findings: list[PIIFinding]
    blocked_reason: str | None


# Built-in PII patterns
_BUILTIN_PATTERNS: list[PIIPattern] = [
    PIIPattern(
        name="email",
        pattern=r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b',
        replacement="[REDACTED_EMAIL]",
        action=PIIAction.WARN,
    ),
    PIIPattern(
        name="te_employee_id",
        pattern=r'\bTE\d{6}\b',
        replacement="[REDACTED_EMPLOYEE_ID]",
        action=PIIAction.WARN,
    ),
    PIIPattern(
        name="phone",
        pattern=r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b',
        replacement="[REDACTED_PHONE]",
        action=PIIAction.WARN,
    ),
    PIIPattern(
        name="phone_international",
        pattern=r'\+\d{1,3}[\s.-]?\(?\d{1,4}\)?[\s.-]?\d{1,4}[\s.-]?\d{1,9}',
        replacement="[REDACTED_PHONE]",
        action=PIIAction.WARN,
    ),
    PIIPattern(
        name="credit_card",
        pattern=(
            r'\b(?:'
            r'4\d{3}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}'       # Visa
            r'|5[1-5]\d{2}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}'  # Mastercard
            r'|3[47]\d{2}[\s-]?\d{6}[\s-]?\d{5}'               # Amex
            r'|6(?:011|5\d{2})[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}'  # Discover
            r')\b'
        ),
        replacement="[REDACTED_CREDIT_CARD]",
        action=PIIAction.WARN,
    ),
    PIIPattern(
        name="ip_address",
        pattern=(
            r'\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}'
            r'(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b'
        ),
        replacement="[REDACTED_IP]",
        action=PIIAction.WARN,
    ),
    PIIPattern(
        name="ssn",
        pattern=r'\b\d{3}-\d{2}-\d{4}\b',
        replacement="[REDACTED_SSN]",
        action=PIIAction.WARN,
    ),
]


class PIISanitizer:
    """Scans text for PII and applies configured actions.

    Usage:
        sanitizer = PIISanitizer(action=PIIAction.REDACT)
        result = sanitizer.check_and_act(prompt_text)
        if not result.allowed:
            raise HTTPException(400, detail=result.blocked_reason)
        use_text = result.sanitized_text or prompt_text
    """

    def __init__(
        self,
        action: PIIAction = PIIAction.WARN,
        custom_patterns: list[PIIPattern] | None = None,
    ) -> None:
        """Initialize sanitizer with default action and optional custom patterns.

        Args:
            action: Default action for built-in patterns (can be overridden per-pattern)
            custom_patterns: Additional tenant-specific patterns
        """
        self._default_action = action
        self._patterns = _BUILTIN_PATTERNS.copy()

        # Override action for built-in patterns if specified
        if action != PIIAction.WARN:
            self._patterns = [
                PIIPattern(
                    name=p.name,
                    pattern=p.pattern,
                    replacement=p.replacement,
                    action=action,
                )
                for p in self._patterns
            ]

        # Add custom patterns
        if custom_patterns:
            self._patterns.extend(custom_patterns)

        # Compile regex patterns
        self._compiled_patterns: list[tuple[PIIPattern, re.Pattern[str]]] = [
            (pattern, re.compile(pattern.pattern)) for pattern in self._patterns
        ]

    def scan(self, text: str) -> PIIScanResult:
        """Scan text for PII patterns without modifying it.

        Returns:
            PIIScanResult with all detected PII findings
        """
        findings: list[PIIFinding] = []

        for pattern, regex in self._compiled_patterns:
            for match in regex.finditer(text):
                findings.append(
                    PIIFinding(
                        pattern_name=pattern.name,
                        match=match.group(),
                        start=match.start(),
                        end=match.end(),
                    )
                )

        has_pii = len(findings) > 0

        if has_pii:
            log.info(
                "pii.scan_detected",
                finding_count=len(findings),
                pattern_names=[f.pattern_name for f in findings],
            )

        return PIIScanResult(has_pii=has_pii, findings=findings)

    def sanitize(self, text: str) -> str:
        """Redact all PII from text, replacing with placeholders.

        This always redacts, regardless of configured action.
        Used when REDACT action is selected.

        Returns:
            Sanitized text with PII replaced by placeholders
        """
        sanitized = text

        # Sort patterns by position (reverse order) to maintain string indices
        findings = self.scan(text).findings
        findings.sort(key=lambda f: f.start, reverse=True)

        for finding in findings:
            # Find the pattern for this finding to get replacement text
            pattern = next(
                (p for p, _ in self._compiled_patterns if p.name == finding.pattern_name),
                None,
            )
            if pattern:
                sanitized = (
                    sanitized[: finding.start]
                    + pattern.replacement
                    + sanitized[finding.end :]
                )

        if findings:
            log.info(
                "pii.sanitized",
                original_length=len(text),
                sanitized_length=len(sanitized),
                redaction_count=len(findings),
            )

        return sanitized

    def check_and_act(self, text: str) -> PIICheckResult:
        """Scan text and apply configured action.

        Actions:
        - REDACT: Replace PII with placeholders, return sanitized text
        - WARN: Log warning, allow original text through
        - BLOCK: Reject request entirely

        Returns:
            PIICheckResult with action taken and results
        """
        scan_result = self.scan(text)

        if not scan_result.has_pii:
            # No PII detected, allow through
            return PIICheckResult(
                allowed=True,
                sanitized_text=None,
                action_taken=self._default_action,
                findings=[],
                blocked_reason=None,
            )

        # PII detected - determine action based on most restrictive pattern
        most_restrictive_action = self._default_action
        for finding in scan_result.findings:
            pattern = next(
                (p for p, _ in self._compiled_patterns if p.name == finding.pattern_name),
                None,
            )
            if pattern:
                # BLOCK > REDACT > WARN
                if pattern.action == PIIAction.BLOCK:
                    most_restrictive_action = PIIAction.BLOCK
                elif (
                    pattern.action == PIIAction.REDACT
                    and most_restrictive_action != PIIAction.BLOCK
                ):
                    most_restrictive_action = PIIAction.REDACT

        if most_restrictive_action == PIIAction.BLOCK:
            log.warning(
                "pii.blocked",
                finding_count=len(scan_result.findings),
                pattern_names=[f.pattern_name for f in scan_result.findings],
            )
            return PIICheckResult(
                allowed=False,
                sanitized_text=None,
                action_taken=PIIAction.BLOCK,
                findings=scan_result.findings,
                blocked_reason=f"PII detected: {', '.join(set(f.pattern_name for f in scan_result.findings))}",
            )

        if most_restrictive_action == PIIAction.REDACT:
            sanitized = self.sanitize(text)
            log.info(
                "pii.redacted",
                finding_count=len(scan_result.findings),
                pattern_names=[f.pattern_name for f in scan_result.findings],
            )
            return PIICheckResult(
                allowed=True,
                sanitized_text=sanitized,
                action_taken=PIIAction.REDACT,
                findings=scan_result.findings,
                blocked_reason=None,
            )

        # WARN action - allow through with warning
        log.warning(
            "pii.warning",
            finding_count=len(scan_result.findings),
            pattern_names=[f.pattern_name for f in scan_result.findings],
        )
        return PIICheckResult(
            allowed=True,
            sanitized_text=None,
            action_taken=PIIAction.WARN,
            findings=scan_result.findings,
            blocked_reason=None,
        )
