"""Tests for health check endpoints."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.infra.health import HealthCheck, ComponentStatus, ComponentHealth
from src.config import Settings, Environment


class TestHealthCheck:
    """Test health check orchestration."""

    @pytest.fixture
    def health_check(self, fake_settings):
        """Create HealthCheck instance."""
        return HealthCheck(fake_settings)

    @pytest.mark.asyncio
    async def test_check_liveness_returns_true_when_healthy(self, health_check):
        """Test /health/live returns True for running process."""
        result = await health_check.check_liveness()
        assert result is True

    @pytest.mark.asyncio
    async def test_check_all_returns_healthy_when_all_components_ok(
        self, health_check
    ):
        """Test that all components healthy results in HEALTHY status."""
        with patch.object(
            health_check, "_check_database", return_value=ComponentHealth(status=ComponentStatus.HEALTHY)
        ):
            with patch.object(
                health_check, "_check_redis", return_value=ComponentHealth(status=ComponentStatus.HEALTHY)
            ):
                with patch.object(
                    health_check, "_check_llm_proxy", return_value=ComponentHealth(status=ComponentStatus.HEALTHY)
                ):
                    with patch.object(
                        health_check, "_check_disk_space", return_value=ComponentHealth(status=ComponentStatus.HEALTHY)
                    ):
                        result = await health_check.check_all()

        assert result.status == ComponentStatus.HEALTHY
        assert "database" in result.components
        assert "llm_proxy" in result.components

    @pytest.mark.asyncio
    async def test_check_all_returns_unhealthy_when_db_fails(
        self, health_check
    ):
        """Test that database failure results in UNHEALTHY status."""
        with patch.object(
            health_check, "_check_database", return_value=ComponentHealth(
                status=ComponentStatus.UNHEALTHY, error="Connection failed"
            )
        ):
            with patch.object(
                health_check, "_check_redis", return_value=ComponentHealth(status=ComponentStatus.HEALTHY)
            ):
                with patch.object(
                    health_check, "_check_llm_proxy", return_value=ComponentHealth(status=ComponentStatus.HEALTHY)
                ):
                    with patch.object(
                        health_check, "_check_disk_space", return_value=ComponentHealth(status=ComponentStatus.HEALTHY)
                    ):
                        result = await health_check.check_all()

        assert result.status == ComponentStatus.UNHEALTHY
        assert result.components["database"].status == ComponentStatus.UNHEALTHY

    @pytest.mark.asyncio
    async def test_check_readiness_requires_db_and_llm(
        self, health_check
    ):
        """Test /health/ready requires critical components (DB, LLM)."""
        # Mock healthy components
        with patch.object(health_check, "check_all") as mock_check_all:
            mock_result = MagicMock()
            mock_result.components = {
                "database": ComponentHealth(status=ComponentStatus.HEALTHY),
                "llm_proxy": ComponentHealth(status=ComponentStatus.HEALTHY),
                "redis": ComponentHealth(status=ComponentStatus.DEGRADED),
                "disk_space": ComponentHealth(status=ComponentStatus.HEALTHY),
            }
            mock_check_all.return_value = mock_result

            result = await health_check.check_readiness()

        # Should be ready even with degraded Redis
        assert result is True

    @pytest.mark.asyncio
    async def test_check_readiness_fails_when_db_unhealthy(
        self, health_check
    ):
        """Test readiness fails when database is unhealthy."""
        with patch.object(health_check, "check_all") as mock_check_all:
            mock_result = MagicMock()
            mock_result.components = {
                "database": ComponentHealth(status=ComponentStatus.UNHEALTHY),
                "llm_proxy": ComponentHealth(status=ComponentStatus.HEALTHY),
                "redis": ComponentHealth(status=ComponentStatus.HEALTHY),
                "disk_space": ComponentHealth(status=ComponentStatus.HEALTHY),
            }
            mock_check_all.return_value = mock_result

            result = await health_check.check_readiness()

        assert result is False

    @pytest.mark.asyncio
    async def test_database_check_succeeds_with_valid_connection(
        self, health_check
    ):
        """Test database check succeeds with working connection."""
        with patch("src.infra.health.get_engine") as mock_get_engine:
            with patch("src.infra.health.AsyncSession") as mock_session_cls:
                mock_session = AsyncMock()
                mock_result = MagicMock()
                mock_result.scalar.return_value = 1
                mock_session.execute.return_value = mock_result
                mock_session_cls.return_value.__aenter__.return_value = mock_session

                result = await health_check._check_database()

        assert result.status == ComponentStatus.HEALTHY
        assert result.latency_ms is not None

    @pytest.mark.asyncio
    async def test_database_check_fails_on_timeout(
        self, health_check
    ):
        """Test database check fails gracefully on timeout."""
        import asyncio

        with patch("src.infra.health.get_engine"):
            with patch("src.infra.health.AsyncSession") as mock_session_cls:
                mock_session = AsyncMock()
                mock_session.execute.side_effect = asyncio.TimeoutError()
                mock_session_cls.return_value.__aenter__.return_value = mock_session

                result = await health_check._check_database()

        assert result.status == ComponentStatus.UNHEALTHY
        assert "timeout" in result.error.lower()

    @pytest.mark.asyncio
    async def test_system_health_to_dict_serializable(
        self, health_check
    ):
        """Test that SystemHealth serializes to JSON-compatible dict."""
        with patch.object(
            health_check, "_check_database", return_value=ComponentHealth(status=ComponentStatus.HEALTHY, latency_ms=10.5)
        ):
            with patch.object(
                health_check, "_check_redis", return_value=ComponentHealth(status=ComponentStatus.UNKNOWN)
            ):
                with patch.object(
                    health_check, "_check_llm_proxy", return_value=ComponentHealth(status=ComponentStatus.HEALTHY, latency_ms=150.0)
                ):
                    with patch.object(
                        health_check, "_check_disk_space", return_value=ComponentHealth(status=ComponentStatus.HEALTHY)
                    ):
                        result = await health_check.check_all()

        health_dict = result.to_dict()
        assert "status" in health_dict
        assert "timestamp" in health_dict
        assert "components" in health_dict
        assert isinstance(health_dict["components"], dict)

        # Verify can be JSON serialized
        import json
        json_str = json.dumps(health_dict)
        assert len(json_str) > 0
