"""Enhanced OpenAPI schema configuration for Enterprise Agent Platform.

Features:
- Custom schema generation with detailed descriptions for all endpoints
- API versioning metadata (v1 current, v2 planned)
- Tag groups with descriptions for logical endpoint organization
- Security schemes: Bearer JWT and X-API-Key
- Custom examples for major request/response bodies
- Rate limit header documentation
- Response schema documentation with error formats

Usage::

    from src.api.openapi_config import configure_openapi
    configure_openapi(app)
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

# ------------------------------------------------------------------ #
# Tag metadata
# ------------------------------------------------------------------ #

OPENAPI_TAGS: list[dict[str, Any]] = [
    {
        "name": "health",
        "description": (
            "Platform health and readiness probes. "
            "Used by load balancers and container orchestrators to determine service availability."
        ),
    },
    {
        "name": "chat",
        "description": (
            "Agent conversation endpoints. "
            "Send messages to AI agents, receive streaming or batch responses. "
            "All conversations are tenant-scoped and audit-logged."
        ),
    },
    {
        "name": "conversations",
        "description": (
            "Conversation history management. "
            "List, retrieve, and delete agent conversation threads. "
            "Supports pagination and filtering by date range."
        ),
    },
    {
        "name": "documents",
        "description": (
            "RAG document management. "
            "Upload, list, retrieve metadata and delete documents from the tenant's "
            "knowledge base. Supports PDF, DOCX, TXT and Markdown."
        ),
    },
    {
        "name": "ingestion",
        "description": (
            "Asynchronous document ingestion pipeline. "
            "Submit documents for chunking, embedding and indexing. "
            "Poll job status and view ingestion logs."
        ),
    },
    {
        "name": "feedback",
        "description": (
            "User feedback collection for RLHF and quality monitoring. "
            "Submit thumbs-up/thumbs-down ratings with optional free-text comments. "
            "Feedback is linked to specific agent responses."
        ),
    },
    {
        "name": "plugins",
        "description": (
            "Plugin lifecycle management. "
            "Enable, disable, and configure plugins per tenant. "
            "Plugins extend agent behaviour via lifecycle hooks."
        ),
    },
    {
        "name": "webhooks",
        "description": (
            "Event-driven webhook subscriptions. "
            "Register HTTPS endpoints to receive real-time event notifications. "
            "Events are HMAC-SHA256 signed for authenticity verification. "
            "Supported events: agent.completed, document.ingested, "
            "feedback.received, compliance.alert, user.created."
        ),
    },
    {
        "name": "compliance",
        "description": (
            "Compliance policy management and audit reporting. "
            "Configure PII detection rules, content filters, and retention policies. "
            "Generate audit reports for SOC-2 and ISO-27001 evidence packages."
        ),
    },
    {
        "name": "compliance-admin",
        "description": (
            "Administrative compliance automation. "
            "Trigger evidence collection, manage compliance schedules, "
            "and view policy violation history."
        ),
    },
    {
        "name": "analytics",
        "description": (
            "Usage analytics and cost reporting. "
            "Query token consumption, request volumes, latency percentiles, "
            "and per-model cost breakdowns."
        ),
    },
    {
        "name": "admin",
        "description": (
            "Platform administration. "
            "User management, role assignments, tenant configuration, "
            "and system-wide settings."
        ),
    },
    {
        "name": "tenant-admin",
        "description": (
            "Tenant administration portal. "
            "Manage tenant settings, subscription plans, API key quotas, "
            "and OIDC identity provider configuration."
        ),
    },
    {
        "name": "api-keys",
        "description": (
            "API key management. "
            "Create, list, rotate, and revoke long-lived API keys for "
            "programmatic access without interactive login."
        ),
    },
    {
        "name": "memory",
        "description": (
            "Agent long-term memory management. "
            "Store, retrieve, and prune semantic memory entries associated "
            "with agents and users."
        ),
    },
    {
        "name": "plans",
        "description": (
            "Subscription plan management. "
            "View available plans, upgrade or downgrade tenant subscriptions, "
            "and check feature entitlements."
        ),
    },
    {
        "name": "playground",
        "description": (
            "Interactive API playground for development and testing. "
            "Try out prompts against all available models without storing conversation history. "
            "Available in development environments only."
        ),
    },
    {
        "name": "cache",
        "description": (
            "Semantic response cache management. "
            "Inspect cache hit rates, manually invalidate entries, "
            "and tune similarity thresholds."
        ),
    },
    {
        "name": "operations",
        "description": (
            "Platform operations endpoints. "
            "Background job status, worker health, and operational tooling "
            "for platform administrators."
        ),
    },
]


# ------------------------------------------------------------------ #
# Security schemes
# ------------------------------------------------------------------ #

SECURITY_SCHEMES: dict[str, Any] = {
    "BearerAuth": {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "JWT",
        "description": (
            "OIDC/JWT Bearer token. "
            "Obtain a token from your identity provider (Keycloak, Auth0, etc.) "
            "and pass it in the ``Authorization: Bearer <token>`` header. "
            "Tokens must include ``tenant_id``, ``role``, and ``sub`` claims."
        ),
    },
    "ApiKeyAuth": {
        "type": "apiKey",
        "in": "header",
        "name": "X-API-Key",
        "description": (
            "Long-lived API key for programmatic access. "
            "Create keys via ``POST /api/v1/keys``. "
            "Pass the key in the ``X-API-Key`` header. "
            "Keys are scoped to a single tenant and can be restricted by IP CIDR."
        ),
    },
}

# Global security requirement: all endpoints accept either scheme
GLOBAL_SECURITY: list[dict[str, list[str]]] = [
    {"BearerAuth": []},
    {"ApiKeyAuth": []},
]


# ------------------------------------------------------------------ #
# Rate limit header documentation
# ------------------------------------------------------------------ #

RATE_LIMIT_HEADERS: dict[str, Any] = {
    "X-RateLimit-Limit": {
        "description": "Maximum number of requests allowed per minute for this tenant.",
        "schema": {"type": "integer", "example": 60},
    },
    "X-RateLimit-Remaining": {
        "description": "Remaining requests in the current rate limit window.",
        "schema": {"type": "integer", "example": 42},
    },
    "X-RateLimit-Reset": {
        "description": "UTC epoch timestamp when the rate limit window resets.",
        "schema": {"type": "integer", "example": 1739808000},
    },
    "Retry-After": {
        "description": (
            "Present only when a 429 Too Many Requests response is returned. "
            "Number of seconds to wait before retrying."
        ),
        "schema": {"type": "integer", "example": 30},
    },
}


# ------------------------------------------------------------------ #
# Standard error response components
# ------------------------------------------------------------------ #

ERROR_RESPONSES: dict[str, Any] = {
    "ValidationError": {
        "description": "Request body or query parameter validation failed.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "detail": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "loc": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                    },
                                    "msg": {"type": "string"},
                                    "type": {"type": "string"},
                                },
                            },
                        }
                    },
                },
                "example": {
                    "detail": [
                        {
                            "loc": ["body", "events"],
                            "msg": "field required",
                            "type": "missing",
                        }
                    ]
                },
            }
        },
    },
    "UnauthorizedError": {
        "description": "Authentication credentials are missing or invalid.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                },
                "example": {"detail": "Not authenticated"},
            }
        },
    },
    "ForbiddenError": {
        "description": "The authenticated user lacks the required permission.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                },
                "example": {"detail": "Insufficient permissions"},
            }
        },
    },
    "NotFoundError": {
        "description": "The requested resource does not exist.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                },
                "example": {"detail": "Resource not found"},
            }
        },
    },
    "RateLimitError": {
        "description": "Too many requests. Back off and retry after the indicated delay.",
        "headers": RATE_LIMIT_HEADERS,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                },
                "example": {"detail": "Rate limit exceeded. Retry after 30 seconds."},
            }
        },
    },
    "InternalError": {
        "description": "An unexpected server-side error occurred.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "properties": {"detail": {"type": "string"}},
                },
                "example": {"detail": "Internal server error"},
            }
        },
    },
}


# ------------------------------------------------------------------ #
# Custom schema generation
# ------------------------------------------------------------------ #


def custom_openapi_schema(app: FastAPI) -> dict[str, Any]:
    """Build an enriched OpenAPI schema for the Enterprise Agent Platform.

    Adds:
    - Detailed API title, description and terms-of-service
    - Contact and license information
    - Multiple server entries (dev, staging, prod)
    - Security schemes (Bearer JWT + API Key)
    - Global security requirement
    - Tag metadata with descriptions
    - Standard error response components
    - Rate limit header documentation in components
    """
    if app.openapi_schema:
        return app.openapi_schema  # type: ignore[return-value]

    schema = get_openapi(
        title="Enterprise Agent Platform API",
        version="1.0.0",
        summary="Multi-tenant enterprise AI agent platform with RAG, audit logging, and OIDC auth.",
        description=_api_description(),
        terms_of_service="https://example.com/terms",
        contact={
            "name": "Platform Engineering",
            "url": "https://example.com/support",
            "email": "platform-eng@example.com",
        },
        license_info={
            "name": "MIT",
            "url": "https://opensource.org/licenses/MIT",
        },
        routes=app.routes,
        tags=OPENAPI_TAGS,
        servers=[
            {
                "url": "http://localhost:8000",
                "description": "Local development server",
            },
            {
                "url": "https://staging-api.example.com",
                "description": "Staging environment",
            },
            {
                "url": "https://api.example.com",
                "description": "Production environment",
            },
        ],
    )

    # Inject security schemes
    schema.setdefault("components", {})
    schema["components"].setdefault("securitySchemes", {})
    schema["components"]["securitySchemes"].update(SECURITY_SCHEMES)

    # Inject error response components
    schema["components"].setdefault("responses", {})
    schema["components"]["responses"].update(ERROR_RESPONSES)

    # Inject rate limit header components
    schema["components"].setdefault("headers", {})
    schema["components"]["headers"].update(RATE_LIMIT_HEADERS)

    # Apply global security requirement to all operations
    schema["security"] = GLOBAL_SECURITY

    # Add versioning extension
    schema["x-api-versions"] = {
        "current": "v1",
        "supported": ["v1"],
        "deprecated": [],
        "planned": ["v2"],
        "v1": {
            "status": "stable",
            "released": "2024-01-01",
            "sunset": None,
            "base_path": "/api/v1",
        },
    }

    # Add rate limit documentation extension
    schema["x-rate-limiting"] = {
        "description": (
            "All API endpoints are subject to per-tenant rate limiting. "
            "Default limit is 60 requests per minute. "
            "Limits are communicated via X-RateLimit-* response headers."
        ),
        "headers": list(RATE_LIMIT_HEADERS.keys()),
    }

    app.openapi_schema = schema
    return schema


def configure_openapi(app: FastAPI) -> None:
    """Attach the custom OpenAPI schema generator to the FastAPI app.

    Call this once during application startup, after all routers are included.

    Example::

        app = FastAPI(...)
        configure_openapi(app)
    """
    app.openapi = lambda: custom_openapi_schema(app)  # type: ignore[method-assign]


# ------------------------------------------------------------------ #
# Internal helpers
# ------------------------------------------------------------------ #


def _api_description() -> str:
    """Return the full Markdown description for the OpenAPI spec."""
    return """
## Overview

The **Enterprise Agent Platform** (EAP) is a multi-tenant AI agent orchestration system
providing:

- **Conversational AI** — Stateful agent conversations with streaming support
- **Retrieval-Augmented Generation** — Upload documents; agents answer grounded in your data
- **Plugin System** — Extend agent behaviour with tenant-scoped plugins
- **Webhook Events** — Real-time event delivery to your systems
- **Compliance & Audit** — Immutable audit logs, PII detection, retention policies
- **OIDC Authentication** — Bring your own identity provider (Keycloak, Auth0, Okta)
- **API Keys** — Long-lived credentials for programmatic/server-to-server access

## Authentication

All endpoints (except health probes) require authentication via one of:

1. **Bearer JWT** — `Authorization: Bearer <oidc_token>`
2. **API Key** — `X-API-Key: <key_value>`

## Tenant Isolation

All resources are strictly tenant-scoped. Every request is bound to a single tenant
derived from the authentication credential. Cross-tenant access is impossible by design.

## Rate Limiting

Requests are rate-limited per tenant (default: 60 req/min). Rate limit status is
communicated via `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `X-RateLimit-Reset`
response headers. When the limit is exceeded a `429 Too Many Requests` response is
returned with a `Retry-After` header.

## Versioning

The API is versioned at the URL path level (`/api/v1/`). Breaking changes will be
introduced only in new major versions. The current stable version is **v1**.

## Webhook Event Signing

Webhook deliveries include an `X-EAP-Signature-256: sha256=<hex>` header. Verify
authenticity by computing `HMAC-SHA256(payload, secret)` and comparing with the header
value using a constant-time comparison function.

## Support

- Documentation: https://docs.example.com
- Status page: https://status.example.com
- Support: platform-eng@example.com
"""
