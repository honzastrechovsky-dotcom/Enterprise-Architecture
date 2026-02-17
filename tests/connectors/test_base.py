"""Tests for base connector infrastructure.

These tests cover connector infrastructure including caching, audit logging, and error handling.
"""

from __future__ import annotations

import uuid

import pytest

from src.connectors.base import (
    AuthType,
    BaseConnector,
    ConnectorConfig,
    ConnectorResult,
    ConnectorStatus,
    RetryConfig,
)


class TestConnectorConfig:
    """Tests for ConnectorConfig validation."""

    def test_valid_config(self) -> None:
        """Test that valid configuration passes validation."""
        config = ConnectorConfig(
            name="test_connector",
            endpoint="https://api.example.com",
            auth_type=AuthType.BEARER,
            timeout_seconds=30.0,
        )
        config.validate()  # Should not raise

    def test_empty_name_fails(self) -> None:
        """Test that empty name raises ValueError."""
        config = ConnectorConfig(
            name="",
            endpoint="https://api.example.com",
        )
        with pytest.raises(ValueError, match="name cannot be empty"):
            config.validate()

    def test_empty_endpoint_fails(self) -> None:
        """Test that empty endpoint raises ValueError."""
        config = ConnectorConfig(
            name="test",
            endpoint="",
        )
        with pytest.raises(ValueError, match="endpoint cannot be empty"):
            config.validate()

    def test_negative_timeout_fails(self) -> None:
        """Test that negative timeout raises ValueError."""
        config = ConnectorConfig(
            name="test",
            endpoint="https://api.example.com",
            timeout_seconds=-1.0,
        )
        with pytest.raises(ValueError, match="Timeout must be positive"):
            config.validate()

    def test_zero_retry_attempts_fails(self) -> None:
        """Test that zero retry attempts raises ValueError."""
        config = ConnectorConfig(
            name="test",
            endpoint="https://api.example.com",
            retry_config=RetryConfig(max_attempts=0),
        )
        with pytest.raises(ValueError, match="Max retry attempts"):
            config.validate()


class TestConnectorResult:
    """Tests for ConnectorResult dataclass."""

    def test_success_result(self) -> None:
        """Test successful result creation."""
        result = ConnectorResult(
            success=True,
            data={"key": "value"},
            metadata={"count": 1},
        )
        assert result.success is True
        assert result.data == {"key": "value"}
        assert result.error is None
        assert result.cached is False

    def test_error_result(self) -> None:
        """Test error result creation."""
        result = ConnectorResult(
            success=False,
            error="Connection failed",
        )
        assert result.success is False
        assert result.error == "Connection failed"
        assert result.data is None

    def test_to_dict(self) -> None:
        """Test result serialization to dictionary."""
        result = ConnectorResult(
            success=True,
            data={"test": "data"},
            metadata={"tenant_id": "123"},
            cached=True,
            classification="class_iii",
        )
        result_dict = result.to_dict()

        assert result_dict["success"] is True
        assert result_dict["data"] == {"test": "data"}
        assert result_dict["metadata"]["tenant_id"] == "123"
        assert result_dict["cached"] is True
        assert result_dict["classification"] == "class_iii"


class MockConnector(BaseConnector):
    """Mock connector for testing base functionality."""

    async def _execute_request(
        self,
        operation: str,
        tenant_id: uuid.UUID,
        params: dict,
    ) -> ConnectorResult:
        """Mock implementation."""
        if operation == "test_operation":
            return ConnectorResult(
                success=True,
                data={"result": "success"},
            )
        return ConnectorResult(
            success=False,
            error=f"Unknown operation: {operation}",
        )

    async def health_check(self) -> ConnectorStatus:
        """Mock health check."""
        self._status = ConnectorStatus.HEALTHY
        return ConnectorStatus.HEALTHY


class TestBaseConnector:
    """Tests for BaseConnector abstract class."""

    @pytest.fixture
    def config(self) -> ConnectorConfig:
        """Fixture providing test connector config."""
        return ConnectorConfig(
            name="mock_connector",
            endpoint="https://api.test.com",
            auth_type=AuthType.BEARER,
            auth_params={"token": "test-token-123"},
        )

    @pytest.fixture
    def connector(self, config: ConnectorConfig) -> MockConnector:
        """Fixture providing mock connector instance."""
        return MockConnector(config)

    @pytest.mark.asyncio
    async def test_connector_initialization(self, connector: MockConnector) -> None:
        """Test connector initializes with valid config."""
        assert connector.config.name == "mock_connector"
        assert connector.status == ConnectorStatus.UNKNOWN

    @pytest.mark.asyncio
    async def test_prepare_bearer_auth_headers(self, connector: MockConnector) -> None:
        """Test bearer token auth header preparation."""
        headers = connector._prepare_auth_headers()
        assert headers["Authorization"] == "Bearer test-token-123"

    @pytest.mark.asyncio
    async def test_prepare_api_key_auth_headers(self) -> None:
        """Test API key auth header preparation."""
        config = ConnectorConfig(
            name="test",
            endpoint="https://api.test.com",
            auth_type=AuthType.API_KEY,
            auth_params={"api_key": "key-123", "api_key_header": "X-Custom-Key"},
        )
        connector = MockConnector(config)
        headers = connector._prepare_auth_headers()
        assert headers["X-Custom-Key"] == "key-123"

    @pytest.mark.asyncio
    async def test_execute_with_context_manager(self, connector: MockConnector) -> None:
        """Test connector execute within async context manager."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        async with connector:
            result = await connector.execute(
                operation="test_operation",
                tenant_id=tenant_id,
                user_id=user_id,
                params={},
            )

        assert result.success is True
        assert result.data == {"result": "success"}

    @pytest.mark.asyncio
    async def test_execute_unknown_operation(self, connector: MockConnector) -> None:
        """Test execute with unknown operation returns error."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        async with connector:
            result = await connector.execute(
                operation="invalid_operation",
                tenant_id=tenant_id,
                user_id=user_id,
                params={},
            )

        assert result.success is False
        assert "Unknown operation" in result.error

    @pytest.mark.asyncio
    async def test_execute_without_context_manager_fails(
        self, connector: MockConnector
    ) -> None:
        """Test that execute without context manager raises error."""
        tenant_id = uuid.uuid4()
        user_id = uuid.uuid4()

        # Should raise because HTTP client not initialized
        result = await connector.execute(
            operation="test_operation",
            tenant_id=tenant_id,
            user_id=user_id,
            params={},
        )

        # Should return error result (not raise exception)
        assert result.success is False
        assert "not initialized" in result.error

    @pytest.mark.asyncio
    async def test_health_check(self, connector: MockConnector) -> None:
        """Test health check returns status."""
        async with connector:
            status = await connector.health_check()
            assert status == ConnectorStatus.HEALTHY
            assert connector.status == ConnectorStatus.HEALTHY
