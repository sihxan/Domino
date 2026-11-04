"""Idempotent Neo4j schema (constraints and indexes) from `0001_schema.cypher`."""

from __future__ import annotations

from pathlib import Path

from domino_core.clients.neo import get_driver, run_cypher_file
from domino_core.settings import Settings

SCHEMA_FILE = Path(__file__).parent / "0001_schema.cypher"


def apply_ddl(*, settings: Settings | None = None) -> int:
    """Apply the Neo4j DDL (safe to repeat); returns the number of statements run."""
    if settings is None:
        return run_cypher_file(SCHEMA_FILE)
    driver = get_driver(settings)
    try:
        return run_cypher_file(SCHEMA_FILE, driver=driver)
    finally:
        driver.close()
