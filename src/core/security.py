"""Security middleware and utilities.

This module provides security hardening middleware for FastAPI applications
following OWASP and general application security best practices.

Key protections:
- Security headers (CSP, HSTS, XSS protections)
- Request size limiting to prevent DoS attacks
- Request ID tracking for audit trails and log correlation
- Log injection prevention via input sanitization
- Content-Type validation for strict input handling
"""

from __future__ import annotations

import re
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

log = structlog.get_logger(__name__)

# Request body size limit (10 MB default)
DEFAULT_MAX_REQUEST_SIZE = 10 * 1024 * 1024

# Control characters to strip for log injection prevention
_CONTROL_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add security headers to all responses (OWASP best practices).

    Implements defense-in-depth protections:
    - X-Content-Type-Options: Prevents MIME type sniffing
    - X-Frame-Options: Prevents clickjacking attacks
    - X-XSS-Protection: Disabled (modern browsers use CSP instead)
    - Strict-Transport-Security: Enforces HTTPS in production
    - Content-Security-Policy: Restricts resource loading
    - Cache-Control: Prevents sensitive data caching
    - Referrer-Policy: Limits referrer information leakage
    - Permissions-Policy: Disables unnecessary browser features
    """

    def __init__(self, app: ASGIApp, *, is_production: bool = False) -> None:
        super().__init__(app)
        self._is_production = is_production

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        response = await call_next(request)

        # X-Content-Type-Options: Prevent MIME sniffing attacks
        response.headers["X-Content-Type-Options"] = "nosniff"

        # X-Frame-Options: Prevent clickjacking
        response.headers["X-Frame-Options"] = "DENY"

        # X-XSS-Protection: Disabled (0) - modern browsers handle this via CSP
        response.headers["X-XSS-Protection"] = "0"

        # Strict-Transport-Security: Force HTTPS in production
        if self._is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        # Content-Security-Policy: Restrict resource loading
        # For API-only applications, we use a strict default-src policy
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"

        # Cache-Control: Prevent caching of API responses
        # API responses often contain sensitive or user-specific data
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        response.headers["Pragma"] = "no-cache"  # HTTP/1.0 backwards compatibility

        # Referrer-Policy: Limit referrer information leakage
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Permissions-Policy: Disable unnecessary browser features
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=(), "
            "magnetometer=(), gyroscope=(), accelerometer=(), "
            "ambient-light-sensor=(), autoplay=(), encrypted-media=(), "
            "picture-in-picture=()"
        )

        return response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """Limit request body size to prevent DoS attacks.

    Large request bodies can exhaust server memory or processing resources.
    This middleware enforces a configurable maximum size and rejects oversized
    requests before they are fully processed.

    Default limit: 10 MB (configurable)
    Status code: 413 Payload Too Large
    """

    def __init__(self, app: ASGIApp, *, max_size: int = DEFAULT_MAX_REQUEST_SIZE) -> None:
        super().__init__(app)
        self._max_size = max_size

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Check Content-Length header if present
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > self._max_size:
            log.warning(
                "security.request_too_large",
                content_length=content_length,
                max_size=self._max_size,
                path=request.url.path,
                method=request.method,
            )
            return JSONResponse(
                status_code=413,
                content={
                    "detail": (
                        f"Request body too large. "
                        f"Maximum allowed: {self._max_size // (1024 * 1024)} MB"
                    )
                },
            )

        # Security: Check for chunked transfer encoding (no Content-Length header)
        # Read body in chunks and count bytes to prevent bypass
        if not content_length and request.method in ("POST", "PUT", "PATCH"):
            total_bytes = 0
            chunks = []

            try:
                async for chunk in request.stream():
                    total_bytes += len(chunk)
                    if total_bytes > self._max_size:
                        log.warning(
                            "security.chunked_request_too_large",
                            total_bytes=total_bytes,
                            max_size=self._max_size,
                            path=request.url.path,
                            method=request.method,
                        )
                        return JSONResponse(
                            status_code=413,
                            content={
                                "detail": (
                                    f"Request body too large. "
                                    f"Maximum allowed: {self._max_size // (1024 * 1024)} MB"
                                )
                            },
                        )
                    chunks.append(chunk)

                # Reconstruct the request body for downstream handlers
                # Note: This requires reconstructing the request with the buffered body
                # Starlette will have already consumed the stream, so we need to create
                # a new scope with the body included
                body = b"".join(chunks)

                # Create a new request with the buffered body
                async def receive():
                    return {"type": "http.request", "body": body}

                # Replace the receive callable
                request._receive = receive  # type: ignore

            except Exception as exc:
                log.error(
                    "security.request_size_check_failed",
                    error=str(exc),
                    path=request.url.path,
                )
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Failed to process request body"},
                )

        return await call_next(request)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Add unique request ID to every request for distributed tracing.

    Request IDs enable:
    - Correlation of logs across services
    - Debugging distributed systems
    - Audit trail tracking
    - Client-side error reporting

    The request ID is:
    - Generated as a UUID4 for uniqueness
    - Stored in request.state.request_id
    - Included in X-Request-ID response header
    - Bound to structlog context for automatic log inclusion
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Generate unique request ID
        request_id = str(uuid.uuid4())

        # Inject into request state for access in route handlers
        request.state.request_id = request_id

        # Bind to structlog context so all logs include request_id
        structlog.contextvars.bind_contextvars(request_id=request_id)

        try:
            response = await call_next(request)

            # Include in response headers for client correlation
            response.headers["X-Request-ID"] = request_id

            return response
        finally:
            # Clear context to avoid leaking request_id to subsequent requests
            structlog.contextvars.clear_contextvars()


def sanitize_log_value(value: str, max_length: int = 500) -> str:
    """Sanitize values before logging to prevent log injection.

    Log injection attacks occur when user-controlled input containing newlines
    or control characters is written to logs, allowing attackers to forge log
    entries or corrupt log parsers.

    This function:
    - Strips null bytes
    - Removes control characters including newlines
    - Truncates to max_length to prevent log flooding

    Args:
        value: The string to sanitize
        max_length: Maximum length after sanitization (default 500)

    Returns:
        Sanitized string safe for logging
    """
    if not value:
        return ""

    # Remove null bytes
    sanitized = value.replace("\x00", "")

    # Remove control characters (including newlines)
    sanitized = _CONTROL_CHARS_PATTERN.sub("", sanitized)

    # Truncate to prevent log flooding
    if len(sanitized) > max_length:
        sanitized = sanitized[:max_length] + "..."

    return sanitized


def validate_content_type(content_type: str | None, allowed: list[str]) -> bool:
    """Validate Content-Type header against an allowlist.

    Enforcing strict Content-Type validation prevents:
    - Content confusion attacks
    - Parser mismatches
    - Unexpected data formats reaching business logic

    This function checks if the request's Content-Type matches one of the
    allowed types. It handles media type parameters (e.g., charset) by
    stripping them before comparison.

    Args:
        content_type: The Content-Type header value from the request
        allowed: List of allowed media types (e.g., ["application/json"])

    Returns:
        True if content_type matches an allowed type, False otherwise

    Example:
        >>> validate_content_type("application/json; charset=utf-8", ["application/json"])
        True
        >>> validate_content_type("text/html", ["application/json"])
        False
    """
    if content_type is None:
        return False

    # Extract media type, stripping parameters
    # "application/json; charset=utf-8" -> "application/json"
    media_type = content_type.split(";")[0].strip().lower()

    # Normalize allowed types for case-insensitive comparison
    normalized_allowed = [t.strip().lower() for t in allowed]

    return media_type in normalized_allowed
