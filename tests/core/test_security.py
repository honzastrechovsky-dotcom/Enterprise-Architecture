"""Tests for security middleware and input validation."""

import pytest

from src.core.security import sanitize_log_value, validate_content_type
from src.core.pii import PIISanitizer, PIIAction, PIIPattern
from src.core.classification import ClassificationPolicy, DataClassification
from src.models.user import UserRole


class TestSanitizeLogValue:
    """Test log injection prevention."""

    def test_removes_null_bytes(self):
        """Test that null bytes are stripped from log values."""
        value = "test\x00value"
        result = sanitize_log_value(value)
        assert "\x00" not in result
        assert result == "testvalue"

    def test_removes_control_characters(self):
        """Test that control characters including newlines are removed."""
        value = "line1\nline2\r\nline3\ttab"
        result = sanitize_log_value(value)
        assert "\n" not in result
        assert "\r" not in result
        assert "\t" not in result

    def test_truncates_long_values(self):
        """Test that values longer than max_length are truncated."""
        value = "a" * 1000
        result = sanitize_log_value(value, max_length=100)
        assert len(result) <= 103  # 100 + "..."
        assert result.endswith("...")

    def test_handles_empty_string(self):
        """Test that empty strings are handled correctly."""
        result = sanitize_log_value("")
        assert result == ""

    def test_preserves_safe_content(self):
        """Test that safe content is preserved."""
        value = "This is a safe log message with 123 numbers"
        result = sanitize_log_value(value)
        # Control chars removed but alphanumeric preserved
        assert "safe" in result
        assert "123" in result


class TestValidateContentType:
    """Test Content-Type validation."""

    def test_accepts_exact_match(self):
        """Test that exact content type matches are accepted."""
        assert validate_content_type("application/json", ["application/json"])

    def test_accepts_with_charset(self):
        """Test that content type with charset parameter is accepted."""
        result = validate_content_type(
            "application/json; charset=utf-8", ["application/json"]
        )
        assert result is True

    def test_rejects_wrong_type(self):
        """Test that wrong content type is rejected."""
        result = validate_content_type("text/html", ["application/json"])
        assert result is False

    def test_rejects_none_content_type(self):
        """Test that None content type is rejected."""
        result = validate_content_type(None, ["application/json"])
        assert result is False

    def test_case_insensitive_matching(self):
        """Test that content type matching is case-insensitive."""
        result = validate_content_type("APPLICATION/JSON", ["application/json"])
        assert result is True

    def test_multiple_allowed_types(self):
        """Test validation with multiple allowed types."""
        allowed = ["application/json", "application/xml"]
        assert validate_content_type("application/json", allowed)
        assert validate_content_type("application/xml", allowed)
        assert not validate_content_type("text/html", allowed)


class TestPIIDetection:
    """Test PII detection and redaction."""

    def test_detects_email_addresses(self):
        """Test that email addresses are detected."""
        sanitizer = PIISanitizer(action=PIIAction.REDACT)
        text = "Contact me at user@example.com for details"
        result = sanitizer.sanitize(text)
        assert "user@example.com" not in result
        assert "[REDACTED_EMAIL]" in result

    def test_detects_phone_numbers(self):
        """Test that phone numbers are detected."""
        sanitizer = PIISanitizer(action=PIIAction.REDACT)
        text = "Call me at 555-123-4567"
        result = sanitizer.sanitize(text)
        assert "555-123-4567" not in result
        assert "[REDACTED_PHONE]" in result

    def test_detects_ssn(self):
        """Test that SSN patterns are detected."""
        sanitizer = PIISanitizer(action=PIIAction.REDACT)
        text = "SSN: 123-45-6789"
        result = sanitizer.sanitize(text)
        assert "123-45-6789" not in result
        assert "[REDACTED_SSN]" in result

    def test_detects_ip_addresses(self):
        """Test that IP addresses are detected."""
        sanitizer = PIISanitizer(action=PIIAction.REDACT)
        text = "Server at 192.168.1.1"
        result = sanitizer.sanitize(text)
        assert "192.168.1.1" not in result
        assert "[REDACTED_IP]" in result

    def test_warn_action_preserves_text(self):
        """Test that WARN action preserves original text."""
        sanitizer = PIISanitizer(action=PIIAction.WARN)
        text = "Email: user@example.com"
        result = sanitizer.check_and_act(text)
        assert result.allowed is True
        assert result.sanitized_text is None  # Original text preserved

    def test_block_action_prevents_processing(self):
        """Test that BLOCK action prevents request processing."""
        sanitizer = PIISanitizer(action=PIIAction.BLOCK)
        text = "Email: user@example.com"
        result = sanitizer.check_and_act(text)
        assert result.allowed is False
        assert result.blocked_reason is not None

    def test_custom_patterns(self):
        """Test that custom PII patterns can be added."""
        custom_pattern = PIIPattern(
            name="badge_number",
            pattern=r"\bBADGE\d{6}\b",
            replacement="[REDACTED_BADGE]",
            action=PIIAction.REDACT,
        )
        sanitizer = PIISanitizer(custom_patterns=[custom_pattern])
        text = "My badge is BADGE123456"
        result = sanitizer.sanitize(text)
        assert "BADGE123456" not in result
        assert "[REDACTED_BADGE]" in result


class TestDataClassification:
    """Test data classification enforcement."""

    @pytest.fixture
    def policy(self):
        """Create ClassificationPolicy instance."""
        return ClassificationPolicy()

    def test_class_i_allows_all_users(self, policy):
        """Test that Class I data is accessible to all authenticated users."""
        result = policy.check_access(
            user_role=UserRole.VIEWER,
            classification=DataClassification.CLASS_I,
        )
        assert result.allowed is True
        assert result.requires_audit is False

    def test_class_ii_allows_all_roles(self, policy):
        """Test that Class II data is accessible to all roles within tenant."""
        for role in [UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMIN]:
            result = policy.check_access(
                user_role=role,
                classification=DataClassification.CLASS_II,
            )
            assert result.allowed is True

    def test_class_iii_requires_operator_role(self, policy, test_user_id):
        """Test that Class III data requires OPERATOR or higher role."""
        # Viewer should be denied
        result = policy.check_access(
            user_role=UserRole.VIEWER,
            classification=DataClassification.CLASS_III,
            document_acl=[test_user_id],
            user_id=test_user_id,
        )
        assert result.allowed is False

        # Operator with ACL should be granted
        result = policy.check_access(
            user_role=UserRole.OPERATOR,
            classification=DataClassification.CLASS_III,
            document_acl=[test_user_id],
            user_id=test_user_id,
        )
        assert result.allowed is True
        assert result.requires_audit is True

    def test_class_iii_enforces_acl(self, policy, test_user_id):
        """Test that Class III enforces document-level ACL."""
        from unittest.mock import MagicMock
        other_user_id = MagicMock()

        result = policy.check_access(
            user_role=UserRole.OPERATOR,
            classification=DataClassification.CLASS_III,
            document_acl=[other_user_id],  # User not in ACL
            user_id=test_user_id,
        )
        assert result.allowed is False

    def test_class_iv_always_requires_approval(self, policy):
        """Test that Class IV data always requires explicit approval."""
        for role in [UserRole.VIEWER, UserRole.OPERATOR, UserRole.ADMIN]:
            result = policy.check_access(
                user_role=role,
                classification=DataClassification.CLASS_IV,
            )
            assert result.allowed is False
            assert result.requires_approval is True
            assert result.requires_audit is True

    def test_classification_requires_audit(self, policy):
        """Test that appropriate classifications require audit logging."""
        # Class I and II don't require special audit
        assert not policy.requires_audit(DataClassification.CLASS_I)
        assert not policy.requires_audit(DataClassification.CLASS_II)

        # Class III and IV require audit
        assert policy.requires_audit(DataClassification.CLASS_III)
        assert policy.requires_audit(DataClassification.CLASS_IV)
