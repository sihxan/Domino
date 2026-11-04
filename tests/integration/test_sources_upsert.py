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
        True,
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


def enabled_of(engine: Engine, source_id: str) -> bool:
    with engine.connect() as conn:
        return bool(conn.execute(text("SELECT enabled FROM sources WHERE id = :id"), {"id": source_id}).scalar_one())


def test_seed_keeps_disabled(engine: Engine) -> None:
    specs = load_sources(ROOT / "sources")
    upsert_sources(specs, engine=engine)
    target = specs[0]
    with connection(engine=engine) as conn:  # what `mark_fail` does after 5 consecutive failures
        conn.execute(text("UPDATE sources SET enabled = false, fail_count = 5 WHERE id = :id"), {"id": target.id})
    upsert_sources(specs, engine=engine)  # `make seed` again, YAML still says enabled: true
    assert target.enabled is True
    assert enabled_of(engine, target.id) is False
    with engine.connect() as conn:
        assert conn.execute(text("SELECT fail_count FROM sources WHERE id = :id"), {"id": target.id}).scalar_one() == 5
    assert count(engine) == 15


def test_new_row_takes_enabled_from_yaml(engine: Engine) -> None:
    base = load_sources(ROOT / "sources")[0]
    off = base.model_copy(update={"id": "src_us_new_off", "enabled": False})
    on = base.model_copy(update={"id": "src_us_new_on", "enabled": True})
    upsert_sources([off, on], engine=engine)
    assert enabled_of(engine, "src_us_new_off") is False
    assert enabled_of(engine, "src_us_new_on") is True
    # re-seeding with flipped YAML values leaves both rows as they are
    upsert_sources([off.model_copy(update={"enabled": True}), on.model_copy(update={"enabled": False})], engine=engine)
    assert enabled_of(engine, "src_us_new_off") is False
    assert enabled_of(engine, "src_us_new_on") is True
