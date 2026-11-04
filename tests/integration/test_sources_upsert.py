"""`upsert_sources` against a migrated Postgres container (week-1 T7)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer

from domino_core.clients.db import connection
from domino_ingest.sources import load_sources, upsert_sources

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    with PostgresContainer("postgres:16.15", driver="psycopg") as pg:
        url = pg.get_connection_url()
        mp = pytest.MonkeyPatch()
        env = {
            "PG_DSN": url,
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
            mp.setenv(k, v)
        try:
            command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        finally:
            mp.undo()
        eng = create_engine(url)
        try:
            yield eng
        finally:
            eng.dispose()


@pytest.fixture(autouse=True)
def clean_sources(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE sources CASCADE"))


def count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM sources")).scalar_one())


def test_upsert_twice_keeps_fifteen_rows(engine: Engine) -> None:
    specs = load_sources(ROOT / "sources")
    assert upsert_sources(specs, engine=engine) == 15
    assert upsert_sources(specs, engine=engine) == 15
    assert count(engine) == 15


def test_upsert_updates_changed_values_by_id(engine: Engine) -> None:
    specs = load_sources(ROOT / "sources")
    upsert_sources(specs, engine=engine)
    changed = specs[0].model_copy(
        update={
            "url": "https://example.com/changed.xml",
            "publisher": "Renamed Publisher",
            "grade": "B",
            "belts": ["semis", "power"],
            "cadence_min": 99,
            "state_controlled": True,
            "wire": "reuters",
            "enabled": False,
        }
    )
    upsert_sources([changed], engine=engine)
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT url, publisher, grade::text, belts, cadence_min, state_controlled, wire, enabled "
                "FROM sources WHERE id = :id"
            ),
            {"id": changed.id},
        ).one()
    assert tuple(row) == (
        "https://example.com/changed.xml",
        "Renamed Publisher",
        "B",
        ["semis", "power"],
        99,
        True,
        "reuters",
        False,
    )
    assert count(engine) == 15


def test_upsert_preserves_operational_state(engine: Engine) -> None:
    specs = load_sources(ROOT / "sources")
    upsert_sources(specs, engine=engine)
    target = specs[0]
    ok_at = datetime(2026, 11, 4, 1, 2, 3, tzinfo=UTC)
    with connection(engine=engine) as conn:
        conn.execute(
            text("UPDATE sources SET last_ok_at = :t, last_error = 'boom', fail_count = 4 WHERE id = :id"),
            {"t": ok_at, "id": target.id},
        )
    upsert_sources([target.model_copy(update={"publisher": "Renamed Publisher"})], engine=engine)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT last_ok_at, last_error, fail_count, publisher FROM sources WHERE id = :id"),
            {"id": target.id},
        ).one()
    assert tuple(row) == (ok_at, "boom", 4, "Renamed Publisher")


def test_upsert_empty_list_is_noop(engine: Engine) -> None:
    assert upsert_sources([], engine=engine) == 0
    assert count(engine) == 0
