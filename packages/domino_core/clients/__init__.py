"""External I/O wrappers (Postgres, Redis, S3/minio, HTTP, Neo4j): connection, retry, error mapping only."""

from __future__ import annotations

from functools import lru_cache

from domino_core.settings import Settings


@lru_cache(maxsize=1)
def default_settings() -> Settings:
    """Settings from the environment, built once per process for the default clients."""
    return Settings()  # type: ignore[call-arg]
