"""Environment-driven application settings (model names live here, not in code)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from domino_core.errors import PermanentError


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", protected_namespaces=(), env_ignore_empty=True)

    env: Literal["dev", "prod"] = "dev"
    # stores
    pg_dsn: SecretStr  # postgresql+psycopg://... (password included)
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: SecretStr
    redis_url: SecretStr  # password may be included
    s3_endpoint: str
    s3_bucket: str = "domino"
    s3_key: SecretStr
    s3_secret: SecretStr
    embedder_url: str = "http://embedder:8080"
    langfuse_host: str
    langfuse_public_key: str
    langfuse_secret_key: SecretStr
    # llm
    llm_backend: Literal["claude_code", "api"] = "claude_code"  # subscription (Claude Code headless) first, api later
    claude_code_oauth_token: SecretStr | None = None  # `claude setup-token` value for containers (claude_code backend)
    anthropic_api_key: SecretStr | None = None  # required only by the api backend
    model_extract: str = "claude-haiku-5-5"
    model_judge: str = "claude-sonnet-5-5"
    model_lead: str = "claude-sonnet-5-5"
    model_worker: str = "claude-haiku-5-5"
    model_editorial: str = "claude-sonnet-5-5"
    # budgets (USD)
    budget_extract_daily: float = 2.0
    budget_report_run: float = 5.0
    budget_judge_daily: float = 1.0
    # pipeline
    chunk_tokens: int = 512
    chunk_overlap: int = 64
    extract_enabled: bool = False  # off until the LLM authentication is decided (ingest trigger and extract_flow)
    simhash_threshold: int = 6  # 64-bit Hamming distance
    resolver_auto_cos: float = 0.92
    resolver_judge_cos: float = 0.80
    confirm_min_publishers: int = 2
    # report
    report_hour_kst: int = 6
    personas_path: Path = Path("packages/domino_report/personas.yaml")
    # delivery
    resend_api_key: SecretStr
    from_email: str
    beta_list_path: Path = Path("beta.yaml")


class _ReportHour(BaseSettings):
    """The one setting `asof_for` needs; same sources (env, `.env`) as `Settings`, no required fields."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    report_hour_kst: int = 6


def get_report_hour_kst() -> int:
    """`REPORT_HOUR_KST` (default 6) without building the full `Settings` — pure code paths need no other env."""
    try:
        return _ReportHour().report_hour_kst
    except ValidationError as e:
        raise PermanentError(f"invalid REPORT_HOUR_KST: {e.errors()[0]['msg']}") from e


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide Settings built once from the environment (tests reset it with `get_settings.cache_clear()`)."""
    return Settings()  # type: ignore[call-arg]
