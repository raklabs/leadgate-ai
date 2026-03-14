"""Application configuration validated via Pydantic Settings (Constitution Section 3.4)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed, validated environment settings with SecretStr protection for credentials."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    APP_ENV: Literal["development", "staging", "production", "test"] = "development"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    DATABASE_PATH: str = Field(default="data/lead_qualifier.db")

    # Security settings
    API_KEY: SecretStr = Field(
        default=SecretStr("dev-internal-api-key-change-me-in-prod-32chars"),
        description="API key required for internal and webhook endpoints",
    )
    WEBHOOK_SECRET: SecretStr = Field(
        default=SecretStr("dev-webhook-hmac-secret-change-me-in-prod"),
        description="HMAC-SHA256 secret for verifying inbound webhooks",
    )
    REQUIRE_WEBHOOK_SIGNATURE: bool = Field(
        default=False,
        description="Enforce HMAC-SHA256 signature verification on /webhook in addition to API key",
    )
    WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS: int = Field(default=300, ge=30, le=3600)
    MAX_PAYLOAD_BYTES: int = Field(default=65536, ge=1024, le=1048576)
    DEDUP_WINDOW_HOURS: int = Field(default=24, ge=1, le=720)
    RATE_LIMIT_REQUESTS_PER_MINUTE: int = Field(default=120, ge=5, le=10000)

    # LLM Extraction settings (Provider-agnostic: ollama | openai | openai_compatible | anthropic | deterministic_local)
    LLM_PROVIDER: Literal[
        "ollama", "openai", "openai_compatible", "anthropic", "deterministic_local"
    ] = "ollama"

    OLLAMA_BASE_URL: str = "http://127.0.0.1:11434"
    OLLAMA_MODEL: str = "qwen3:4b-instruct"

    OPENAI_API_KEY: SecretStr | None = None
    OPENAI_MODEL: str = "gpt-4o-mini"
    OPENAI_BASE_URL: str = "https://api.openai.com/v1"

    ANTHROPIC_API_KEY: SecretStr | None = None
    ANTHROPIC_MODEL: str = "claude-3-5-sonnet-latest"
    ANTHROPIC_BASE_URL: str = "https://api.anthropic.com/v1"

    LLM_TIMEOUT_SECONDS: float = Field(default=90.0, ge=1.0, le=300.0)
    LLM_MAX_RETRIES: int = Field(default=3, ge=1, le=6)

    # CRM Sync settings (mock | hubspot | gohighlevel | salesforce)
    CRM_PROVIDER: Literal["mock", "hubspot", "gohighlevel", "salesforce"] = "mock"
    CRM_API_KEY: SecretStr | None = None
    CRM_BASE_URL: str = "https://api.hubapi.com"
    CRM_TIMEOUT_SECONDS: float = Field(default=10.0, ge=1.0, le=60.0)
    CRM_MAX_RETRIES: int = Field(default=3, ge=1, le=6)

    # Outbound Email Delivery settings (Strictly gated by human approval)
    EMAIL_PROVIDER: Literal["local_outbox", "smtp", "resend"] = "local_outbox"
    EMAIL_FROM_ADDRESS: str = "advisory@intelligentstack.ai"
    RESEND_API_KEY: SecretStr | None = None
    SMTP_HOST: str = "localhost"
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: SecretStr | None = None
    SMTP_USE_TLS: bool = True

    # Scoring Configuration
    SCORING_RULES_PATH: str = "config/scoring_rules.yaml"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings instance."""
    return Settings()

