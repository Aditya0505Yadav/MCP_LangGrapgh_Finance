"""Validated environment configuration (single source of truth).

Credentials and connection strings are injected here through runtime
environment configuration — never hard-coded, never placed into workflow
checkpoint state. The MCP server process reads the same settings object for the
handful of fields it needs (DB, data mode, bind address, fault injection).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Anthropic Claude ---
    anthropic_api_key: str = Field(default="replace_locally")
    claude_model: str = Field(default="claude-opus-5-5")
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    claude_max_tokens: int = Field(default=2000, ge=256, le=128000)

    # --- PostgreSQL ---
    database_url: str = Field(
        default="postgresql://finance:finance@localhost:5432/finance_demo"
    )
    app_pool_min_size: int = Field(default=1, ge=1)
    app_pool_max_size: int = Field(default=5, ge=1)
    checkpointer_pool_min_size: int = Field(default=1, ge=1)
    checkpointer_pool_max_size: int = Field(default=3, ge=1)

    # --- MCP server (Streamable HTTP) ---
    mcp_server_url: str = Field(default="http://localhost:8001/mcp")
    mcp_host: str = Field(default="0.0.0.0")
    mcp_port: int = Field(default=8001)

    # --- Application API ---
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    # --- Data mode ---
    data_mode: Literal["fixture", "live"] = "fixture"
    financial_data_api_key: str | None = None

    # --- Reliability ---
    max_external_attempts: int = Field(default=3, ge=1, le=10)

    # --- Observability ---
    log_payloads: bool = True
    payload_max_chars: int = Field(default=4000, ge=256)
    display_timezone: str = "America/Chicago"

    # --- Versioning ---
    workflow_version: str = "v1"
    code_version: str = "dev"

    # --- Fault injection (demo/tests only) ---
    fail_at: str | None = None
    mcp_fault_transient_symbol: str | None = None
    mcp_fault_transient_times: int = 0

    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        # langgraph's postgres checkpointer and psycopg both accept the
        # `postgresql://` scheme. Normalize the common SQLAlchemy-style
        # `postgresql+psycopg://` form to the plain driver URL.
        return v.replace("postgresql+psycopg://", "postgresql://")

    @property
    def fail_at_normalized(self) -> str | None:
        return self.fail_at.strip().lower() if self.fail_at else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor so the .env file is parsed once per process."""
    return Settings()
