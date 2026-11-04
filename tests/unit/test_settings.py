"""domino_core.settings: environment-driven Settings with tech §2.2 defaults."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from domino_core.settings import Settings, get_settings

REQUIRED_ENV = {
    "PG_DSN": "postgresql+psycopg://dbuser:pg-pass-value@localhost:5432/domino",
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "neo4j-secret-value",
    "REDIS_URL": "redis://:redis-pass-value@localhost:6379/0",
    "S3_ENDPOINT": "http://localhost:9000",
    "S3_KEY": "s3-key-value",
    "S3_SECRET": "s3-secret-value",
    "LANGFUSE_HOST": "http://localhost:3000",
    "LANGFUSE_PUBLIC_KEY": "pk-test",
    "LANGFUSE_SECRET_KEY": "langfuse-secret-value",
    "ANTHROPIC_API_KEY": "anthropic-secret-value",
    "RESEND_API_KEY": "resend-secret-value",
    "FROM_EMAIL": "report@example.com",
}
SECRET_VALUES = [
    "pg-pass-value",
    "redis-pass-value",
    "neo4j-secret-value",
    "s3-key-value",
    "s3-secret-value",
    "langfuse-secret-value",
    "anthropic-secret-value",
    "resend-secret-value",
]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ["ENV", "MODEL_EXTRACT", "MODEL_JUDGE", "MODEL_LEAD", "MODEL_WORKER", "MODEL_EDITORIAL"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def make() -> Settings:
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_built_from_env(env: None) -> None:
    s = make()
    assert s.pg_dsn.get_secret_value() == REQUIRED_ENV["PG_DSN"]
    assert s.redis_url.get_secret_value() == REQUIRED_ENV["REDIS_URL"]
    assert s.neo4j_uri == REQUIRED_ENV["NEO4J_URI"]
    assert s.from_email == REQUIRED_ENV["FROM_EMAIL"]
    assert s.anthropic_api_key.get_secret_value() == "anthropic-secret-value"


def test_env_overrides_default(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_EXTRACT", "claude-other")
    monkeypatch.setenv("CHUNK_TOKENS", "256")
    s = make()
    assert s.model_extract == "claude-other"
    assert s.chunk_tokens == 256


def test_missing_required_fails(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import ValidationError

    monkeypatch.delenv("PG_DSN")
    with pytest.raises(ValidationError):
        make()


def test_defaults_match_tech_spec(env: None) -> None:
    s = make()
    assert s.env == "dev"
    assert s.s3_bucket == "domino"
    assert s.embedder_url == "http://embedder:8080"
    assert s.model_extract == "claude-haiku-5-5"
    assert s.model_judge == "claude-sonnet-5-5"
    assert s.model_lead == "claude-sonnet-5-5"
    assert s.model_worker == "claude-haiku-5-5"
    assert s.model_editorial == "claude-sonnet-5-5"
    assert s.budget_extract_daily == 2.0
    assert s.budget_report_run == 5.0
    assert s.budget_judge_daily == 1.0
    assert s.chunk_tokens == 512
    assert s.chunk_overlap == 64
    assert s.simhash_threshold == 6
    assert s.resolver_auto_cos == 0.92
    assert s.resolver_judge_cos == 0.80
    assert s.confirm_min_publishers == 2
    assert s.report_hour_kst == 6
    assert s.personas_path == Path("packages/domino_report/personas.yaml")
    assert s.beta_list_path == Path("beta.yaml")


def test_env_example_copy_keeps_defaults(env: None, tmp_path: Path) -> None:
    """A .env made from .env.example with only required keys filled must not override defaults with ''."""
    example = Path(__file__).resolve().parents[2] / ".env.example"
    lines = [
        f"{key}{REQUIRED_ENV[key[:-1]]}" if key[:-1] in REQUIRED_ENV else key
        for key in example.read_text(encoding="utf-8").splitlines()
    ]
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(lines), encoding="utf-8")
    s = Settings(_env_file=env_file)  # type: ignore[call-arg]
    assert s.env == "dev"
    assert s.model_extract == "claude-haiku-5-5"
    assert s.chunk_tokens == 512
    assert s.budget_extract_daily == 2.0


def test_empty_process_env_keeps_default(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_LEAD", "")
    monkeypatch.setenv("CHUNK_OVERLAP", "")
    s = make()
    assert s.model_lead == "claude-sonnet-5-5"
    assert s.chunk_overlap == 64


def test_secrets_are_secretstr(env: None) -> None:
    s = make()
    for field in [
        "pg_dsn",
        "redis_url",
        "neo4j_password",
        "s3_key",
        "s3_secret",
        "langfuse_secret_key",
        "anthropic_api_key",
        "resend_api_key",
    ]:
        assert isinstance(getattr(s, field), SecretStr)


def test_secrets_not_in_repr(env: None) -> None:
    s = make()
    text = repr(s) + str(s) + repr(s.model_dump())
    for secret in SECRET_VALUES:
        assert secret not in text


def test_get_settings_is_cached_per_process(env: None) -> None:
    assert get_settings() is get_settings()


def test_get_settings_cache_clear_rebuilds_from_env(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    monkeypatch.setenv("CHUNK_TOKENS", "256")
    assert get_settings() is first
    get_settings.cache_clear()
    assert get_settings().chunk_tokens == 256


def test_default_settings_is_the_process_wide_get_settings(env: None) -> None:
    from domino_core.clients import default_settings

    assert default_settings() is get_settings()


def test_dsn_not_in_repr(env: None) -> None:
    """L5/⑦: the DSNs carry passwords; neither repr, str nor model_dump text may show them."""
    s = make()
    assert isinstance(s.pg_dsn, SecretStr)
    assert isinstance(s.redis_url, SecretStr)
    text = repr(s) + str(s) + repr(s.model_dump()) + s.model_dump_json()
    assert "pg-pass-value" not in text
    assert "redis-pass-value" not in text


def test_settings_has_no_tz(env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """⑧: the system TZ variable must not matter; time zone is the timeutil.KST constant."""
    monkeypatch.setenv("TZ", "UTC")
    assert "tz" not in Settings.model_fields
    assert not hasattr(make(), "tz")
    example = Path(__file__).resolve().parents[2] / ".env.example"
    assert not [line for line in example.read_text(encoding="utf-8").splitlines() if line.startswith("TZ=")]
