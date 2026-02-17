"""
Application configuration via pydantic-settings.

All settings are loaded from environment variables (or a .env file in dev).
This is the single source of truth for configuration - nothing is hardcoded
elsewhere.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    PROD = "prod"
    TEST = "test"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Application
    # ------------------------------------------------------------------ #
    environment: Environment = Environment.DEV
    secret_key: SecretStr = Field(
        default="dev-secret-key-not-for-production",
        description="HMAC secret for internal token signing",
    )
    debug: bool = False

    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #
    database_url: str = Field(
        default="postgresql+asyncpg://app:app_password@localhost:5432/enterprise_agents",
        description="Async SQLAlchemy database URL",
    )
    db_echo_sql: bool = False  # Set True for SQL query logging in dev

    # ------------------------------------------------------------------ #
    # LiteLLM Proxy
    # ------------------------------------------------------------------ #
    litellm_base_url: str = Field(
        default="http://localhost:4000",
        description="LiteLLM proxy base URL",
    )
    litellm_api_key: SecretStr = Field(
        default=SecretStr("sk-dev-key"),
        description="API key for LiteLLM proxy",
    )
    litellm_default_model: str = Field(
        default="openai/gpt-4o-mini",
        description="Default LLM model identifier (LiteLLM format)",
    )
    litellm_embedding_model: str = Field(
        default="openai/text-embedding-3-small",
        description="Embedding model identifier (LiteLLM format)",
    )

    # ------------------------------------------------------------------ #
    # OIDC / Auth
    # ------------------------------------------------------------------ #
    oidc_issuer_url: str = Field(
        default="http://localhost:8080/realms/dev",
        description="OIDC issuer URL for JWKS discovery",
    )
    oidc_client_id: str = Field(
        default="enterprise-agents",
        description="Expected 'aud' claim in JWT tokens",
    )
    oidc_audience: str = Field(
        default="enterprise-agents-api",
        description="Expected audience in JWT tokens",
    )
    # Dev mode: skip OIDC JWKS fetch, validate with symmetric secret
    dev_jwt_secret: SecretStr = Field(
        default=SecretStr("dev-only-jwt-secret-not-for-production"),
        description="Symmetric secret for JWT validation in dev mode only",
    )
    # Air-gapped / offline OIDC: read JWKS from a local file instead of HTTP
    jwks_local_path: str | None = Field(
        default=None,
        description=(
            "Path to local JWKS JSON file for air-gapped OIDC. "
            "When set, JWKS is read from disk instead of making HTTP requests to the IdP. "
            "Useful for on-premise deployments where the IdP is not reachable at startup."
        ),
    )

    # ------------------------------------------------------------------ #
    # Public URL (used for SAML SP metadata / ACS URL generation)
    # ------------------------------------------------------------------ #
    public_base_url: str = Field(
        default="http://localhost:8000",
        description="Public base URL of this platform, used for SAML SP metadata",
    )

    # ------------------------------------------------------------------ #
    # CORS
    # ------------------------------------------------------------------ #
    cors_allowed_origins: list[str] = Field(
        default=["http://localhost:5173", "http://localhost:3000"],
        description="Allowed CORS origins. In production, set to actual frontend URLs.",
    )

    # ------------------------------------------------------------------ #
    # Rate Limiting
    # ------------------------------------------------------------------ #
    rate_limit_per_minute: int = Field(
        default=60,
        ge=1,
        description="Max requests per user per minute",
    )

    # ------------------------------------------------------------------ #
    # RAG / Document Processing
    # ------------------------------------------------------------------ #
    chunk_size_tokens: int = Field(default=512, ge=64, le=2048)
    chunk_overlap_tokens: int = Field(default=50, ge=0, le=256)
    vector_top_k: int = Field(default=5, ge=1, le=20)
    embedding_dimensions: int = Field(default=1536, description="Must match embedding model output")

    # ------------------------------------------------------------------ #
    # Infrastructure
    # ------------------------------------------------------------------ #
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for distributed rate limiting and caching",
    )
    background_worker_concurrency: int = Field(
        default=4,
        ge=1,
        le=32,
        description="Number of concurrent background worker coroutines",
    )
    otlp_endpoint: str = Field(
        default="http://localhost:4317",
        description="OpenTelemetry OTLP collector endpoint",
    )
    enable_telemetry: bool = Field(
        default=False,
        description="Enable OpenTelemetry distributed tracing",
    )

    # ------------------------------------------------------------------ #
    # MFA
    # ------------------------------------------------------------------ #
    mfa_enabled: bool = Field(
        default=False,
        description=(
            "Enable TOTP-based MFA validation for CRITICAL risk operation approvals. "
            "When False, any non-empty mfa_code is accepted (backward-compat mode)."
        ),
    )
    mfa_static_code: str | None = Field(
        default=None,
        description=(
            "Fallback static MFA code used when a user has no per-user TOTP secret "
            "configured.  Only relevant when mfa_enabled=True.  Leave unset to reject "
            "approvals from users without a TOTP secret."
        ),
    )

    # ------------------------------------------------------------------ #
    # Notifications
    # ------------------------------------------------------------------ #
    smtp_host: str | None = Field(
        default=None,
        description="SMTP server hostname.  Leave unset to disable email notifications.",
    )
    smtp_port: int = Field(
        default=587,
        ge=1,
        le=65535,
        description="SMTP server port (587 = STARTTLS, 465 = SSL/TLS, 25 = plain).",
    )
    smtp_user: str | None = Field(
        default=None,
        description="SMTP login username.",
    )
    smtp_password: SecretStr | None = Field(
        default=None,
        description="SMTP login password.",
    )
    smtp_from: str = Field(
        default="noreply@enterprise-agents.local",
        description="From address used in outgoing notification emails.",
    )
    smtp_use_tls: bool = Field(
        default=False,
        description="Use implicit TLS (port 465).  When False, STARTTLS is used if available.",
    )
    webhook_url: str | None = Field(
        default=None,
        description=(
            "Generic webhook URL for notifications (Slack incoming-webhook, "
            "Microsoft Teams connector, etc.).  Leave unset to disable."
        ),
    )

    # ------------------------------------------------------------------ #
    # Connector Configuration
    # ------------------------------------------------------------------ #
    sap_endpoint: str = Field(
        default="http://localhost:8001/sap/opu/odata/sap",
        description="SAP OData v2 service root URL",
    )
    sap_auth_type: str = Field(
        default="basic",
        description="SAP auth type: none, basic, bearer, oauth2, api_key",
    )
    sap_username: str = Field(
        default="",
        description="SAP basic-auth username",
    )
    sap_password: SecretStr = Field(
        default=SecretStr(""),
        description="SAP basic-auth password",
    )
    sap_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="SAP API key (used when sap_auth_type=api_key)",
    )
    sap_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="SAP connector request timeout in seconds",
    )

    mes_endpoint: str = Field(
        default="http://localhost:8002",
        description="MES REST API base URL",
    )
    mes_auth_type: str = Field(
        default="api_key",
        description="MES auth type: none, basic, bearer, oauth2, api_key",
    )
    mes_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="MES API key",
    )
    mes_timeout_seconds: float = Field(
        default=30.0,
        gt=0,
        description="MES connector request timeout in seconds",
    )

    # ------------------------------------------------------------------ #
    # Model Routing & Token Economy
    # ------------------------------------------------------------------ #
    model_routing_enabled: bool = Field(
        default=True,
        description="Enable intelligent model tier routing based on complexity",
    )
    model_light: str = Field(
        default="ollama/qwen2.5:7b",
        description="Light tier model for simple tasks (intent classification, PII)",
    )
    model_standard: str = Field(
        default="ollama/qwen2.5:32b",
        description="Standard tier model for most agent tasks",
    )
    model_heavy: str = Field(
        default="vllm/qwen2.5:72b",
        description="Heavy tier model for complex reasoning (thinking tools, security)",
    )
    token_budget_daily: int = Field(
        default=1_000_000,
        ge=1000,
        description="Default daily token budget per tenant",
    )
    token_budget_monthly: int = Field(
        default=20_000_000,
        ge=10000,
        description="Default monthly token budget per tenant",
    )

    # ------------------------------------------------------------------ #
    # Derived / Computed
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _set_debug_from_env(self) -> Settings:
        if self.environment == Environment.DEV:
            self.debug = True
        return self

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> Settings:
        """Refuse to start in production with default/insecure secrets."""
        if self.environment == Environment.PROD:
            _insecure = {"dev-secret-key-not-for-production", "dev-only-jwt-secret-not-for-production"}
            if self.secret_key.get_secret_value() in _insecure:
                raise ValueError("SECRET_KEY must be changed from default for production")
            if self.dev_jwt_secret.get_secret_value() in _insecure:
                raise ValueError("DEV_JWT_SECRET must be changed from default for production")
            if self.litellm_api_key.get_secret_value() == "sk-dev-key":
                raise ValueError("LITELLM_API_KEY must be changed from default for production")
        return self

    @property
    def is_dev(self) -> bool:
        return self.environment in (Environment.DEV, Environment.TEST)

    @property
    def is_prod(self) -> bool:
        return self.environment == Environment.PROD


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached settings singleton.

    Use FastAPI dependency injection via Depends(get_settings) in endpoints,
    or call directly in non-request contexts (startup, scripts).
    """
    return Settings()
