"""External I/O wrappers (Postgres, Redis, S3/minio, HTTP, Neo4j): connection, retry, error mapping only."""

from __future__ import annotations

from domino_core.settings import Settings, get_settings


def default_settings() -> Settings:
    """Settings for the default clients: the process-wide `get_settings()` instance."""
    return get_settings()
