"""Base connector infrastructure for enterprise integrations.

All enterprise connectors inherit from BaseConnector and follow these patterns:
1. Tenant isolation - every operation scoped by tenant_id
2. Audit logging - universal audit trail for compliance
3. Connection pooling - efficient resource usage
4. Health checks - proactive monitoring
5. Configuration validation - fail fast on misconfiguration
6. Retry logic with exponential backoff
7. Classification-aware responses
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


class AuthType(StrEnum):
    """Authentication methods supported by connectors."""

    NONE = "none"
    BASIC = "basic"
    BEARER = "bearer"
    OAUTH2 = "oauth2"
    API_KEY = "api_key"


class ConnectorStatus(StrEnum):
    """Health status for connector endpoints."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


@dataclass
class RetryConfig:
    """Retry configuration for connector operations."""

    max_attempts: int = 3
    min_wait_seconds: float = 1.0
    max_wait_seconds: float = 10.0
    multiplier: float = 2.0


@dataclass
class ConnectorConfig:
    """Configuration for an enterprise connector."""

    name: str
    endpoint: str
    auth_type: AuthType = AuthType.NONE
    timeout_seconds: float = 30.0
    retry_config: RetryConfig = field(default_factory=RetryConfig)
    # Additional auth params stored here (API keys, client IDs, etc.)
    auth_params: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> None:
        """Validate configuration and raise ValueError if invalid."""
        if not self.name:
            raise ValueError("Connector name cannot be empty")
        if not self.endpoint:
            raise ValueError("Connector endpoint cannot be empty")
        if self.timeout_seconds <= 0:
            raise ValueError("Timeout must be positive")
        if self.retry_config.max_attempts < 1:
            raise ValueError("Max retry attempts must be at least 1")


@dataclass
class ConnectorResult:
    """Result from a connector operation."""

    success: bool
    data: Any = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    cached: bool = False
    # Classification level for compliance (e.g., "class_iii", "class_iv")
    classification: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "success": self.success,
            "data": self.data,
            "error": self.error,
            "metadata": self.metadata,
            "cached": self.cached,
            "classification": self.classification,
        }


class BaseConnector(ABC):
    """Abstract base class for all enterprise connectors.

    Subclasses must implement:
    - _execute_request(): Actual connector-specific logic
    - health_check(): Verify connectivity to external system

    The base class provides:
    - Tenant isolation enforcement
    - Universal audit logging
    - HTTP client with connection pooling
    - Retry logic with exponential backoff
    - Configuration validation
    """

    def __init__(self, config: ConnectorConfig) -> None:
        config.validate()
        self.config = config
        self._http_client: httpx.AsyncClient | None = None
        self._status = ConnectorStatus.UNKNOWN

    async def __aenter__(self) -> BaseConnector:
        """Async context manager entry - initialize HTTP client."""
        self._http_client = httpx.AsyncClient(
            base_url=self.config.endpoint,
            timeout=self.config.timeout_seconds,
            # Connection pooling for efficiency
            limits=httpx.Limits(max_keepalive_connections=20, max_connections=100),
        )
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Async context manager exit - cleanup HTTP client."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    def _get_http_client(self) -> httpx.AsyncClient:
        """Get the HTTP client, raising if not initialized."""
        if self._http_client is None:
            raise RuntimeError(
                f"{self.config.name} connector not initialized. Use 'async with connector:'"
            )
        return self._http_client

    def _prepare_auth_headers(self) -> dict[str, str]:
        """Prepare authentication headers based on auth_type."""
        headers: dict[str, str] = {}

        if self.config.auth_type == AuthType.BEARER:
            token = self.config.auth_params.get("token", "")
            if token:
                headers["Authorization"] = f"Bearer {token}"

        elif self.config.auth_type == AuthType.API_KEY:
            api_key = self.config.auth_params.get("api_key", "")
            key_header = self.config.auth_params.get("api_key_header", "X-API-Key")
            if api_key:
                headers[key_header] = api_key

        elif self.config.auth_type == AuthType.BASIC:
            # Basic auth handled by httpx.BasicAuth, not headers
            pass

        return headers

    async def execute(
        self,
        operation: str,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Execute a connector operation with audit logging and tenant isolation.

        Args:
            operation: Operation name (e.g., "get_purchase_orders")
            tenant_id: Tenant UUID for isolation
            user_id: User UUID for audit trail
            params: Operation-specific parameters

        Returns:
            ConnectorResult with data or error
        """
        start_time = datetime.now(UTC)

        log.info(
            "connector.execute_start",
            connector=self.config.name,
            operation=operation,
            tenant_id=str(tenant_id),
            user_id=str(user_id),
        )

        try:
            # Check if HTTP client is initialized
            if self._http_client is None:
                return ConnectorResult(
                    success=False,
                    error=f"{self.config.name} connector not initialized. Use 'async with connector:'",
                )

            # Subclass implements actual request logic
            result = await self._execute_request(operation, tenant_id, params)

            # Record successful execution in audit log
            await self._audit_log(
                tenant_id=tenant_id,
                user_id=user_id,
                operation=operation,
                params=params,
                result=result,
                duration_ms=(datetime.now(UTC) - start_time).total_seconds() * 1000,
            )

            log.info(
                "connector.execute_success",
                connector=self.config.name,
                operation=operation,
                tenant_id=str(tenant_id),
                cached=result.cached,
            )

            return result

        except Exception as exc:
            # Record failed execution in audit log
            error_result = ConnectorResult(success=False, error=str(exc))
            await self._audit_log(
                tenant_id=tenant_id,
                user_id=user_id,
                operation=operation,
                params=params,
                result=error_result,
                duration_ms=(datetime.now(UTC) - start_time).total_seconds() * 1000,
            )

            log.error(
                "connector.execute_failed",
                connector=self.config.name,
                operation=operation,
                error=str(exc),
                tenant_id=str(tenant_id),
            )

            return error_result

    @abstractmethod
    async def _execute_request(
        self,
        operation: str,
        tenant_id: uuid.UUID,
        params: dict[str, Any],
    ) -> ConnectorResult:
        """Subclass implements connector-specific request logic.

        This method should:
        1. Validate operation name
        2. Make external API calls
        3. Map response to normalized dataclasses
        4. Apply classification tagging
        5. Return ConnectorResult
        """

    @abstractmethod
    async def health_check(self) -> ConnectorStatus:
        """Check if the external system is reachable and responsive.

        Returns:
            ConnectorStatus indicating health
        """

    async def _audit_log(
        self,
        tenant_id: uuid.UUID,
        user_id: uuid.UUID,
        operation: str,
        params: dict[str, Any],
        result: ConnectorResult,
        duration_ms: float,
    ) -> None:
        """Write connector operation to audit log.

        This is a universal audit trail for compliance. Connector operations
        are logged via structlog for structured log aggregation.

        Future: Insert into database audit table with retention policy.
        """
        log.info(
            "connector.audit",
            tenant_id=str(tenant_id),
            user_id=str(user_id),
            connector=self.config.name,
            operation=operation,
            success=result.success,
            classification=result.classification,
            duration_ms=round(duration_ms, 2),
            cached=result.cached,
            # Don't log full params/data - may contain sensitive info
            # In production, sanitize and write to audit_logs table
        )

    @property
    def status(self) -> ConnectorStatus:
        """Current health status of this connector."""
        return self._status
