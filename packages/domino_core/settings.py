"""Environment-driven application settings (model names live here, not in code)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", protected_namespaces=(), env_ignore_empty=True)

    env: Literal["dev", "prod"] = "dev"
    tz: str = "Asia/Seoul"
    # stores
    pg_dsn: str  # postgresql+psycopg://...
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: SecretStr
    redis_url: str
    s3_endpoint: str
    s3_bucket: str = "domino"
    s3_key: SecretStr
    s3_secret: SecretStr
    embedder_url: str = "http://embedder:8080"
    langfuse_host: str
    langfuse_public_key: str
    langfuse_secret_key: SecretStr
    # llm
    anthropic_api_key: SecretStr
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
