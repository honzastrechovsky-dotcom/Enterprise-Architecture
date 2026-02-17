"""Input validation utilities.

This module provides validators and sanitizers for user-supplied input
following general application security best practices. All user input
must be validated before processing to prevent injection attacks, path
traversal, and other security vulnerabilities.

Validation philosophy:
- Whitelist allowed patterns (explicit allow)
- Reject known-bad patterns (explicit deny)
- Fail closed (reject on ambiguity)
- Validate early (at API boundary)
- Normalize before validation (strip, lowercase, etc.)
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Maximum lengths for user-supplied input fields
MAX_CHAT_MESSAGE_LENGTH = 10_000
MAX_SEARCH_QUERY_LENGTH = 1_000
MAX_FILENAME_LENGTH = 255

# Allowed file extensions (allowlist)
ALLOWED_EXTENSIONS = {
    ".txt",
    ".pdf",
    ".docx",
    ".xlsx",
    ".pptx",
    ".md",
    ".csv",
    ".json",
    ".xml",
}

# Path traversal patterns (denylist)
_PATH_TRAVERSAL_PATTERN = re.compile(r"\.\./|\.\.\\|\.\.")


class ValidationError(ValueError):
    """Raised when input validation fails.

    This is a ValueError subclass to maintain compatibility with
    FastAPI's automatic validation error handling.
    """

    pass


class InputValidator:
    """Validates and sanitizes user input.

    All methods are static to allow use without instantiation.
    Each validator:
    1. Normalizes input (strip whitespace, remove null bytes)
    2. Validates against constraints
    3. Returns sanitized value or raises ValidationError

    Usage:
        message = InputValidator.validate_chat_message(user_input)
    """

    @staticmethod
    def validate_chat_message(message: str) -> str:
        """Validate chat message input.

        Chat messages are user-generated free text that will be:
        - Logged to audit tables
        - Sent to LLM APIs
        - Displayed in UI

        Validation rules:
        - Strip leading/trailing whitespace
        - Remove null bytes (security)
        - Reject empty messages
        - Enforce maximum length

        Args:
            message: The raw user message string

        Returns:
            Sanitized message ready for processing

        Raises:
            ValidationError: If validation fails
        """
        if not isinstance(message, str):
            raise ValidationError("Message must be a string")

        # Normalize: strip whitespace and null bytes
        sanitized = message.strip().replace("\x00", "")

        # Reject empty messages
        if not sanitized:
            raise ValidationError("Message cannot be empty")

        # Enforce maximum length
        if len(sanitized) > MAX_CHAT_MESSAGE_LENGTH:
            raise ValidationError(
                f"Message too long. Maximum {MAX_CHAT_MESSAGE_LENGTH} characters allowed."
            )

        return sanitized

    @staticmethod
    def validate_uuid(value: str) -> uuid.UUID:
        """Validate UUID format.

        UUIDs are used for tenant IDs, user IDs, document IDs, etc.
        This validator ensures the input is a valid UUID v4 format
        before using it in database queries or API calls.

        Args:
            value: String representation of a UUID

        Returns:
            Parsed UUID object

        Raises:
            ValidationError: If the UUID format is invalid
        """
        if not isinstance(value, str):
            raise ValidationError("UUID must be a string")

        try:
            # UUID constructor validates format and raises ValueError on failure
            parsed = uuid.UUID(value)
        except ValueError as exc:
            raise ValidationError(f"Invalid UUID format: {exc}") from exc

        return parsed

    @staticmethod
    def validate_filename(filename: str) -> str:
        """Validate uploaded filename.

        Filenames from user uploads must be validated to prevent:
        - Path traversal attacks (../ sequences)
        - Null byte injection
        - Disallowed file types
        - Excessively long names

        Validation rules:
        - Strip whitespace
        - Remove null bytes
        - Reject path traversal patterns
        - Allowlist allowed extensions
        - Enforce maximum length

        Args:
            filename: The uploaded filename (basename only, no path)

        Returns:
            Sanitized filename ready for storage

        Raises:
            ValidationError: If validation fails
        """
        if not isinstance(filename, str):
            raise ValidationError("Filename must be a string")

        # Normalize: strip whitespace and null bytes
        sanitized = filename.strip().replace("\x00", "")

        # Reject empty filenames
        if not sanitized:
            raise ValidationError("Filename cannot be empty")

        # Reject path traversal attempts
        if _PATH_TRAVERSAL_PATTERN.search(sanitized):
            log.warning("security.path_traversal_attempt", filename=sanitized)
            raise ValidationError("Filename contains disallowed path sequences")

        # Reject path separators
        if "/" in sanitized or "\\" in sanitized:
            log.warning("security.path_separator_in_filename", filename=sanitized)
            raise ValidationError("Filename cannot contain path separators")

        # Enforce maximum length
        if len(sanitized) > MAX_FILENAME_LENGTH:
            raise ValidationError(
                f"Filename too long. Maximum {MAX_FILENAME_LENGTH} characters allowed."
            )

        # Allowlist allowed extensions
        # Use pathlib for cross-platform extension extraction
        extension = Path(sanitized).suffix.lower()
        if not extension:
            raise ValidationError("Filename must have an extension")

        if extension not in ALLOWED_EXTENSIONS:
            raise ValidationError(
                f"File type '{extension}' not allowed. "
                f"Allowed types: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            )

        return sanitized

    @staticmethod
    def validate_search_query(query: str) -> str:
        """Validate search query input.

        Search queries are used for RAG vector similarity search and
        traditional text search. They must be validated to prevent:
        - Injection attacks
        - Excessive resource consumption
        - Control character injection

        Validation rules:
        - Strip whitespace
        - Remove null bytes
        - Remove control characters
        - Reject empty queries
        - Enforce maximum length

        Args:
            query: The raw search query string

        Returns:
            Sanitized query ready for search operations

        Raises:
            ValidationError: If validation fails
        """
        if not isinstance(query, str):
            raise ValidationError("Query must be a string")

        # Normalize: strip whitespace and null bytes
        sanitized = query.strip().replace("\x00", "")

        # Remove control characters (can interfere with search engines or logs)
        sanitized = "".join(char for char in sanitized if ord(char) >= 32 or char in "\t\n")

        # Reject empty queries
        if not sanitized:
            raise ValidationError("Search query cannot be empty")

        # Enforce maximum length
        if len(sanitized) > MAX_SEARCH_QUERY_LENGTH:
            raise ValidationError(
                f"Search query too long. Maximum {MAX_SEARCH_QUERY_LENGTH} characters allowed."
            )

        return sanitized
