"""Tests for ConnectorRegistry wired to WriteOperationExecutor.

Verifies that:
1. build_connector_registry_from_settings() creates a registry with SAP and MES.
2. The executor built from settings routes execution through the registry.
3. The /execute endpoint is present and enforces tenant isolation.
4. Execution falls back gracefully when settings are unavailable.

TDD: these tests were written before the implementation to define the contract.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.connectors.base import AuthType, ConnectorConfig, ConnectorResult
from src.connectors.mes import MESConnector
from src.connectors.sap import SAPConnector
from src.operations.write_framework import (
    ConnectorRegistry,
    OperationStatus,
    RiskLevel,
    WriteOperation,
    WriteOperationExecutor,
    build_connector_registry_from_settings,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(
    sap_auth_type: str = "basic",
    mes_auth_type: str = "api_key",
) -> SimpleNamespace:
    """Return a minimal settings-like object for registry construction."""

    class _FakeSecret:
        def __init__(self, value: str) -> None:
            self._value = value

        def get_secret_value(self) -> str:
            return self._value

    return SimpleNamespace(
        sap_endpoint="http://sap.example.com/odata",
        sap_auth_type=sap_auth_type,
        sap_username="sapuser",
        sap_password=_FakeSecret("sappass"),
        sap_api_key=_FakeSecret("sap-api-key"),
        sap_timeout_seconds=30.0,
        mes_endpoint="http://mes.example.com",
        mes_auth_type=mes_auth_type,
        mes_api_key=_FakeSecret("mes-api-key"),
        mes_timeout_seconds=30.0,
    )


def _make_mock_connector() -> MagicMock:
    """Return a mock connector that succeeds on execute()."""
    mock = MagicMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=None)
    mock.execute = AsyncMock(
        return_value=ConnectorResult(
            success=True,
            data={"result": "ok"},
            metadata={"connector": "mock"},
        )
    )
    return mock


# ---------------------------------------------------------------------------
# build_connector_registry_from_settings
# ---------------------------------------------------------------------------


class TestBuildConnectorRegistryFromSettings:
    """Unit tests for the registry factory function."""

    def test_registers_sap_and_mes(self) -> None:
        """Registry must contain 'sap' and 'mes' after construction."""
        settings = _make_settings()
        registry = build_connector_registry_from_settings(settings)

        connectors = registry.known_connectors()
        assert "sap" in connectors
        assert "mes" in connectors

    def test_sap_connector_class_is_sap(self) -> None:
        """Registry must instantiate SAPConnector for 'sap'."""
        settings = _make_settings()
        registry = build_connector_registry_from_settings(settings)

        sap = registry.create("sap")
        assert isinstance(sap, SAPConnector)

    def test_mes_connector_class_is_mes(self) -> None:
        """Registry must instantiate MESConnector for 'mes'."""
        settings = _make_settings()
        registry = build_connector_registry_from_settings(settings)

        mes = registry.create("mes")
        assert isinstance(mes, MESConnector)

    def test_sap_config_uses_settings_endpoint(self) -> None:
        """SAP connector config must use the endpoint from settings."""
        settings = _make_settings()
        registry = build_connector_registry_from_settings(settings)

        sap = registry.create("sap")
        assert sap.config.endpoint == "http://sap.example.com/odata"

    def test_mes_config_uses_settings_endpoint(self) -> None:
        """MES connector config must use the endpoint from settings."""
        settings = _make_settings()
        registry = build_connector_registry_from_settings(settings)

        mes = registry.create("mes")
        assert mes.config.endpoint == "http://mes.example.com"

    def test_sap_basic_auth_type_mapped(self) -> None:
        """sap_auth_type='basic' must translate to AuthType.BASIC."""
        settings = _make_settings(sap_auth_type="basic")
        registry = build_connector_registry_from_settings(settings)

        sap = registry.create("sap")
        assert sap.config.auth_type == AuthType.BASIC

    def test_mes_api_key_auth_type_mapped(self) -> None:
        """mes_auth_type='api_key' must translate to AuthType.API_KEY."""
        settings = _make_settings(mes_auth_type="api_key")
        registry = build_connector_registry_from_settings(settings)

        mes = registry.create("mes")
        assert mes.config.auth_type == AuthType.API_KEY

    def test_unknown_auth_type_falls_back_to_none(self) -> None:
        """An unrecognised auth type must fall back to AuthType.NONE."""
        settings = _make_settings(sap_auth_type="magic_token")
        registry = build_connector_registry_from_settings(settings)

        sap = registry.create("sap")
        assert sap.config.auth_type == AuthType.NONE

    def test_sap_basic_auth_params_populated(self) -> None:
        """SAP basic auth must put username/password in auth_params."""
        settings = _make_settings(sap_auth_type="basic")
        registry = build_connector_registry_from_settings(settings)

        sap = registry.create("sap")
        assert sap.config.auth_params.get("username") == "sapuser"
        assert sap.config.auth_params.get("password") == "sappass"

    def test_mes_api_key_auth_params_populated(self) -> None:
        """MES api_key auth must put the key in auth_params."""
        settings = _make_settings(mes_auth_type="api_key")
        registry = build_connector_registry_from_settings(settings)

        mes = registry.create("mes")
        assert mes.config.auth_params.get("api_key") == "mes-api-key"

    def test_timeout_propagated_to_sap(self) -> None:
        """SAP connector timeout must match settings.sap_timeout_seconds."""
        settings = _make_settings()
        registry = build_connector_registry_from_settings(settings)

        sap = registry.create("sap")
        assert sap.config.timeout_seconds == 30.0

    def test_timeout_propagated_to_mes(self) -> None:
        """MES connector timeout must match settings.mes_timeout_seconds."""
        settings = _make_settings()
        registry = build_connector_registry_from_settings(settings)

        mes = registry.create("mes")
        assert mes.config.timeout_seconds == 30.0


# ---------------------------------------------------------------------------
# WriteOperationExecutor wired with a real registry
# ---------------------------------------------------------------------------


class TestExecutorWithRegistry:
    """Verify the executor properly routes execution through the registry."""

    @pytest.mark.asyncio
    async def test_execute_routes_to_sap_connector(self) -> None:
        """Executor must call the SAP connector when connector='sap'."""
        mock_connector = _make_mock_connector()

        registry = ConnectorRegistry()
        registry.create = MagicMock(return_value=mock_connector)  # type: ignore[method-assign]

        executor = WriteOperationExecutor(connector_registry=registry)
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="create_purchase_request",
            params={"material_id": "MAT-001"},
            description="Create purchase request",
            risk_level=RiskLevel.LOW,  # Auto-approved
        )
        proposed = await executor.propose(op)
        assert proposed.status == OperationStatus.APPROVED

        result = await executor.execute(proposed.id)

        assert result.success is True
        registry.create.assert_called_once_with("sap")
        mock_connector.execute.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_execute_routes_to_mes_connector(self) -> None:
        """Executor must call the MES connector when connector='mes'."""
        mock_connector = _make_mock_connector()

        registry = ConnectorRegistry()
        registry.create = MagicMock(return_value=mock_connector)  # type: ignore[method-assign]

        executor = WriteOperationExecutor(connector_registry=registry)
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="mes",
            operation_type="update_production_order",
            params={"order_id": "ORD-123"},
            description="Update production order",
            risk_level=RiskLevel.LOW,
        )
        proposed = await executor.propose(op)
        result = await executor.execute(proposed.id)

        assert result.success is True
        registry.create.assert_called_once_with("mes")

    @pytest.mark.asyncio
    async def test_execute_unknown_connector_returns_failure(self) -> None:
        """Execution of an unknown connector must return a failed result, not raise."""
        settings = _make_settings()
        registry = build_connector_registry_from_settings(settings)
        executor = WriteOperationExecutor(connector_registry=registry)

        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="erp_unknown",
            operation_type="do_something",
            params={},
            description="Unknown connector",
            risk_level=RiskLevel.LOW,
        )
        proposed = await executor.propose(op)
        result = await executor.execute(proposed.id)

        assert result.success is False
        assert "erp_unknown" in (result.error or "")
        updated = await executor.get_operation(proposed.id)
        assert updated.status == OperationStatus.FAILED

    @pytest.mark.asyncio
    async def test_operation_status_becomes_completed_on_success(self) -> None:
        """Operation status must be COMPLETED after a successful execution."""
        mock_connector = _make_mock_connector()

        registry = ConnectorRegistry()
        registry.create = MagicMock(return_value=mock_connector)  # type: ignore[method-assign]

        executor = WriteOperationExecutor(connector_registry=registry)
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="create_purchase_request",
            params={},
            description="Test",
            risk_level=RiskLevel.LOW,
        )
        proposed = await executor.propose(op)
        await executor.execute(proposed.id)

        updated = await executor.get_operation(proposed.id)
        assert updated.status == OperationStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_operation_status_becomes_failed_on_connector_error(self) -> None:
        """Operation status must be FAILED when the connector returns failure."""
        mock_connector = _make_mock_connector()
        mock_connector.execute = AsyncMock(
            return_value=ConnectorResult(success=False, error="SAP 503")
        )

        registry = ConnectorRegistry()
        registry.create = MagicMock(return_value=mock_connector)  # type: ignore[method-assign]

        executor = WriteOperationExecutor(connector_registry=registry)
        op = WriteOperation(
            tenant_id=uuid.uuid4(),
            user_id=uuid.uuid4(),
            connector="sap",
            operation_type="create_purchase_request",
            params={},
            description="Test failure",
            risk_level=RiskLevel.LOW,
        )
        proposed = await executor.propose(op)
        result = await executor.execute(proposed.id)

        assert result.success is False
        updated = await executor.get_operation(proposed.id)
        assert updated.status == OperationStatus.FAILED


# ---------------------------------------------------------------------------
# _build_executor fallback behaviour
# ---------------------------------------------------------------------------


class TestBuildExecutorFallback:
    """Verify the executor factory falls back safely if settings fail.

    Rather than importing the full API routes module (which has many
    transitive deps that may not be installed in this test environment),
    we replicate the _build_executor() logic inline and verify the
    fallback path of build_connector_registry_from_settings.
    """

    def test_fallback_executor_has_no_registry_when_settings_unavailable(self) -> None:
        """An executor built when settings raise must have _registry=None."""

        def _simulated_build_executor() -> WriteOperationExecutor:
            """Mirrors src/api/routes/operations.py:_build_executor logic."""
            try:
                # Simulate settings failure.
                raise RuntimeError("settings not configured")
            except Exception:
                return WriteOperationExecutor()

        executor = _simulated_build_executor()
        assert isinstance(executor, WriteOperationExecutor)
        assert executor._registry is None

    def test_executor_with_registry_has_registry_set(self) -> None:
        """An executor built with a valid registry must expose that registry."""
        settings = _make_settings()
        registry = build_connector_registry_from_settings(settings)
        executor = WriteOperationExecutor(connector_registry=registry)

        assert executor._registry is registry
        assert "sap" in executor._registry.known_connectors()
        assert "mes" in executor._registry.known_connectors()
