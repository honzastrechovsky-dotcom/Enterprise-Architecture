"""Tests for PII sanitization patterns and scanner.

Covers built-in patterns including credit card, international phone,
and validated IP address detection.
"""

from __future__ import annotations

import pytest

from src.core.pii import PIIAction, PIISanitizer


class TestCreditCardPattern:
    """Credit card detection for Visa, Mastercard, Amex, Discover."""

    @pytest.mark.parametrize(
        "card",
        [
            "4111111111111111",       # Visa
            "4111-1111-1111-1111",    # Visa with dashes
            "4111 1111 1111 1111",    # Visa with spaces
            "5111111111111111",       # Mastercard (51xx)
            "5511111111111111",       # Mastercard (55xx)
            "371449635398431",        # Amex
            "3714-496353-98431",      # Amex with dashes
            "6011111111111111",       # Discover (6011)
            "6511111111111111",       # Discover (65xx)
        ],
    )
    def test_detects_credit_cards(self, card: str) -> None:
        sanitizer = PIISanitizer()
        result = sanitizer.scan(f"Card: {card}")
        names = [f.pattern_name for f in result.findings]
        assert "credit_card" in names, f"Failed to detect card: {card}"

    @pytest.mark.parametrize(
        "text",
        [
            "1234567890123456",  # Not a valid prefix
            "411111111111",     # Too short for Visa
        ],
    )
    def test_rejects_non_credit_cards(self, text: str) -> None:
        sanitizer = PIISanitizer()
        result = sanitizer.scan(text)
        names = [f.pattern_name for f in result.findings]
        assert "credit_card" not in names

    def test_redacts_credit_card(self) -> None:
        sanitizer = PIISanitizer(action=PIIAction.REDACT)
        text = "Pay with 4111111111111111 please"
        sanitized = sanitizer.sanitize(text)
        assert "4111111111111111" not in sanitized
        assert "[REDACTED_CREDIT_CARD]" in sanitized


class TestInternationalPhonePattern:
    """International phone number detection."""

    @pytest.mark.parametrize(
        "phone",
        [
            "+1-555-123-4567",
            "+44 20 7946 0958",
            "+49.30.12345678",
            "+81 3 1234 5678",
        ],
    )
    def test_detects_international_phones(self, phone: str) -> None:
        sanitizer = PIISanitizer()
        result = sanitizer.scan(f"Call {phone}")
        names = [f.pattern_name for f in result.findings]
        assert "phone_international" in names, f"Failed to detect: {phone}"


class TestIPAddressValidation:
    """IP address pattern validates 0-255 octets."""

    @pytest.mark.parametrize(
        "ip",
        [
            "192.168.1.1",
            "10.0.0.1",
            "255.255.255.255",
            "0.0.0.0",
        ],
    )
    def test_detects_valid_ips(self, ip: str) -> None:
        sanitizer = PIISanitizer()
        result = sanitizer.scan(f"Server at {ip}")
        names = [f.pattern_name for f in result.findings]
        assert "ip_address" in names, f"Failed to detect: {ip}"

    @pytest.mark.parametrize(
        "bad_ip",
        [
            "999.999.999.999",
            "256.1.1.1",
            "1.1.1.300",
        ],
    )
    def test_rejects_invalid_ips(self, bad_ip: str) -> None:
        sanitizer = PIISanitizer()
        result = sanitizer.scan(bad_ip)
        names = [f.pattern_name for f in result.findings]
        assert "ip_address" not in names, f"Should reject invalid IP: {bad_ip}"


class TestExistingPatterns:
    """Ensure existing patterns still work after modifications."""

    def test_email_detection(self) -> None:
        sanitizer = PIISanitizer()
        result = sanitizer.scan("user@example.com")
        assert result.has_pii
        assert any(f.pattern_name == "email" for f in result.findings)

    def test_ssn_detection(self) -> None:
        sanitizer = PIISanitizer()
        result = sanitizer.scan("SSN: 123-45-6789")
        assert result.has_pii
        assert any(f.pattern_name == "ssn" for f in result.findings)

    def test_us_phone_detection(self) -> None:
        sanitizer = PIISanitizer()
        result = sanitizer.scan("Call 555-123-4567")
        assert result.has_pii
        assert any(f.pattern_name == "phone" for f in result.findings)

    def test_no_pii_clean_text(self) -> None:
        sanitizer = PIISanitizer()
        result = sanitizer.scan("This is a normal sentence with no PII.")
        assert not result.has_pii
