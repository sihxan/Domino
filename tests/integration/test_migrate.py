"""Migration 0001 (Postgres) and the Neo4j DDL against throwaway containers (week-1 T5)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, text
from testcontainers.community.neo4j import Neo4jContainer
from testcontainers.community.postgres import PostgresContainer

from domino_core.clients.neo import get_driver
from domino_core.settings import Settings
from domino_graph.ddl import apply_ddl

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "spec" / "tech-phase0.md"
NEO4J_PASSWORD = "test-password-1"  # throwaway container

SPEC_TYPES = {"source_grade", "doc_type"}
INDEX_NAMES = {
    "evidence_edge",
    "event_time",
    "event_sched",
    "causes_temporal",
    "causes_status",
    "entity_text",
    "entity_embedding",
    "evidence_embedding",
    "storyline_embedding",
}


def spec_tables() -> set[str]:
    """Table names from every CREATE TABLE in tech-phase0.md §3."""
    body = SPEC.read_text(encoding="utf-8")
    section = body.split("## 3. 데이터 모델 — Postgres", 1)[1].split("## 4. 데이터 모델 — Neo4j", 1)[0]
    return set(re.findall(r"^CREATE TABLE (\w+)", section, flags=re.MULTILINE))


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16.15", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="module")
def neo4j_settings() -> Iterator[Settings]:
    with Neo4jContainer("neo4j:5.26.32-community", password=NEO4J_PASSWORD) as neo:
        yield Settings(  # type: ignore[call-arg]
            _env_file=None,
            pg_dsn="postgresql+psycopg://unused",
            neo4j_uri=neo.get_connection_url(),
            neo4j_user="neo4j",
            neo4j_password=SecretStr(NEO4J_PASSWORD),
            redis_url="redis://unused",
            s3_endpoint="http://unused",
            s3_key=SecretStr("unused"),
            s3_secret=SecretStr("unused"),
            langfuse_host="http://unused",
            langfuse_public_key="unused",
            langfuse_secret_key=SecretStr("unused"),
            anthropic_api_key=SecretStr("unused"),
            resend_api_key=SecretStr("unused"),
            from_email="unused@example.com",
        )


@pytest.fixture
def alembic_cfg(pg_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Alembic config whose env.py reads the DSN from Settings (via environment variables)."""
    env = {
        "PG_DSN": pg_url,
        "NEO4J_URI": "bolt://unused",
        "NEO4J_USER": "unused",
        "NEO4J_PASSWORD": "unused",
        "REDIS_URL": "redis://unused",
        "S3_ENDPOINT": "http://unused",
        "S3_KEY": "unused",
        "S3_SECRET": "unused",
        "LANGFUSE_HOST": "http://unused",
        "LANGFUSE_PUBLIC_KEY": "unused",
        "LANGFUSE_SECRET_KEY": "unused",
        "ANTHROPIC_API_KEY": "unused",
        "RESEND_API_KEY": "unused",
        "FROM_EMAIL": "unused@example.com",
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return Config(str(ROOT / "alembic.ini"))


def public_tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names(schema="public"))
    finally:
        engine.dispose()


def enum_types(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("SELECT typname FROM pg_type WHERE typtype = 'e'"))
            return {r[0] for r in rows}
    finally:
        engine.dispose()


def test_spec_has_twenty_tables() -> None:
    assert len(spec_tables()) == 20


def test_upgrade_head_creates_exactly_the_spec_tables(alembic_cfg: Config, pg_url: str) -> None:
    command.upgrade(alembic_cfg, "head")
    assert public_tables(pg_url) == spec_tables() | {"alembic_version"}
    assert enum_types(pg_url) == SPEC_TYPES


def test_downgrade_base_then_upgrade_head(alembic_cfg: Config, pg_url: str) -> None:
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "base")
    assert public_tables(pg_url) == {"alembic_version"}
    assert enum_types(pg_url) == set()
    command.upgrade(alembic_cfg, "head")
    assert public_tables(pg_url) == spec_tables() | {"alembic_version"}


def test_neo4j_ddl_is_idempotent(neo4j_settings: Settings) -> None:
    apply_ddl(settings=neo4j_settings)
    apply_ddl(settings=neo4j_settings)
    driver = get_driver(neo4j_settings)
    try:
        with driver.session() as s:
            constraints = s.run("SHOW CONSTRAINTS YIELD name").values()
            indexes = {r[0] for r in s.run("SHOW INDEXES YIELD name").values()}
    finally:
        driver.close()
    assert len(constraints) == 7
    assert indexes >= INDEX_NAMES
