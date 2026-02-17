"""Tests for application configuration."""

import pytest
from pydantic import ValidationError

from src.config import Settings, Environment, get_settings


class TestSettings:
    """Test Settings model and validation."""

    def test_default_settings_load_correctly(self):
        """Test that default settings are loaded with correct values."""
        settings = Settings()

        assert settings.environment == Environment.DEV
        assert settings.debug is True  # Auto-set from DEV environment
        assert settings.database_url is not None
        assert settings.litellm_base_url is not None

    def test_production_validation_rejects_default_secrets(self):
        """Test that production environment rejects insecure default secrets."""
        with pytest.raises(ValidationError) as exc_info:
            Settings(environment=Environment.PROD)

        error_str = str(exc_info.value)
        assert "SECRET_KEY must be changed" in error_str or "production" in error_str.lower()

    def test_is_dev_property_returns_true_for_dev(self):
        """Test that is_dev property correctly identifies dev environment."""
        settings = Settings(environment=Environment.DEV)
        assert settings.is_dev is True
        assert settings.is_prod is False

    def test_is_dev_property_returns_true_for_test(self):
        """Test that is_dev property includes test environment."""
        settings = Settings(environment=Environment.TEST)
        assert settings.is_dev is True
        assert settings.is_prod is False

    def test_is_prod_property_returns_true_for_prod(self):
        """Test that is_prod property correctly identifies production."""
        settings = Settings(
            environment=Environment.PROD,
            secret_key="production-secret-key-that-is-secure",
            dev_jwt_secret="production-jwt-secret-that-is-secure",
            litellm_api_key="sk-production-key",
        )
        assert settings.is_prod is True
        assert settings.is_dev is False

    def test_environment_enum_values(self):
        """Test that Environment enum has expected values."""
        assert Environment.DEV == "dev"
        assert Environment.PROD == "prod"
        assert Environment.TEST == "test"

    def test_debug_auto_enabled_in_dev(self):
        """Test that debug is automatically enabled in dev environment."""
        settings = Settings(environment=Environment.DEV, debug=False)
        # _set_debug_from_env should override to True
        assert settings.debug is True

    def test_debug_not_auto_enabled_in_prod(self):
        """Test that debug is not automatically enabled in production."""
        settings = Settings(
            environment=Environment.PROD,
            debug=False,
            secret_key="production-secret-key",
            dev_jwt_secret="production-jwt-secret",
            litellm_api_key="sk-production-key",
        )
        assert settings.debug is False

    def test_database_url_configuration(self):
        """Test that database URL can be configured."""
        custom_db_url = "postgresql+asyncpg://custom:pass@db:5432/custom_db"
        settings = Settings(database_url=custom_db_url)
        assert settings.database_url == custom_db_url

    def test_rate_limit_configuration(self):
        """Test that rate limiting can be configured."""
        settings = Settings(rate_limit_per_minute=120)
        assert settings.rate_limit_per_minute == 120

    def test_rate_limit_minimum_validation(self):
        """Test that rate limit enforces minimum value."""
        with pytest.raises(ValidationError):
            Settings(rate_limit_per_minute=0)

    def test_chunk_size_validation(self):
        """Test that chunk size is within valid range."""
        settings = Settings(chunk_size_tokens=512)
        assert settings.chunk_size_tokens == 512

        with pytest.raises(ValidationError):
            Settings(chunk_size_tokens=32)  # Too small

        with pytest.raises(ValidationError):
            Settings(chunk_size_tokens=5000)  # Too large

    def test_model_routing_configuration(self):
        """Test that model routing can be configured."""
        settings = Settings(
            model_routing_enabled=True,
            model_light="ollama/qwen2.5:7b",
            model_standard="ollama/qwen2.5:32b",
            model_heavy="vllm/qwen2.5:72b",
        )
        assert settings.model_routing_enabled is True
        assert "7b" in settings.model_light
        assert "32b" in settings.model_standard
        assert "72b" in settings.model_heavy

    def test_get_settings_returns_singleton(self):
        """Test that get_settings returns cached singleton instance."""
        settings1 = get_settings()
        settings2 = get_settings()
        assert settings1 is settings2

    def test_token_budget_configuration(self):
        """Test that token budgets can be configured."""
        settings = Settings(
            token_budget_daily=2_000_000,
            token_budget_monthly=50_000_000,
        )
        assert settings.token_budget_daily == 2_000_000
        assert settings.token_budget_monthly == 50_000_000

    def test_token_budget_minimum_validation(self):
        """Test that token budgets enforce minimum values."""
        with pytest.raises(ValidationError):
            Settings(token_budget_daily=500)  # Too small

        with pytest.raises(ValidationError):
            Settings(token_budget_monthly=5000)  # Too small

    def test_oidc_configuration(self):
        """Test that OIDC settings can be configured."""
        settings = Settings(
            oidc_issuer_url="https://auth.example.com/realms/production",
            oidc_client_id="my-client",
            oidc_audience="my-api",
        )
        assert settings.oidc_issuer_url == "https://auth.example.com/realms/production"
        assert settings.oidc_client_id == "my-client"
        assert settings.oidc_audience == "my-api"

    def test_telemetry_configuration(self):
        """Test that telemetry can be enabled/disabled."""
        settings = Settings(
            enable_telemetry=True,
            otlp_endpoint="http://collector:4317",
        )
        assert settings.enable_telemetry is True
        assert settings.otlp_endpoint == "http://collector:4317"

    def test_background_worker_concurrency_validation(self):
        """Test that worker concurrency is within valid range."""
        settings = Settings(background_worker_concurrency=8)
        assert settings.background_worker_concurrency == 8

        with pytest.raises(ValidationError):
            Settings(background_worker_concurrency=0)

        with pytest.raises(ValidationError):
            Settings(background_worker_concurrency=100)
