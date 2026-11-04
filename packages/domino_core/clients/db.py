"""Postgres via SQLAlchemy 2 Core: engine from `settings.pg_dsn` and a transactional connection."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import sqlalchemy.exc as sa_exc
from sqlalchemy import Connection, Engine, create_engine

from domino_core.clients import default_settings
from domino_core.errors import PermanentError, TransientError
from domino_core.settings import Settings

_engine: Engine | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    """SQLAlchemy engine for `settings.pg_dsn`; with no argument, a process-wide default."""
    global _engine
    if settings is None and _engine is not None:
        return _engine
    s = settings or default_settings()
    engine = create_engine(s.pg_dsn.get_secret_value(), pool_pre_ping=True, hide_parameters=True)
    if settings is None:
        _engine = engine
    return engine


@contextmanager
def connection(*, engine: Engine | None = None) -> Iterator[Connection]:
    """Connection inside one transaction (commit on exit, rollback on error); DB errors are mapped."""
    try:
        with (engine or get_engine()).begin() as conn:
            yield conn
    except (sa_exc.OperationalError, sa_exc.TimeoutError) as e:
        raise TransientError(f"postgres {type(e).__name__}") from e
    except sa_exc.SQLAlchemyError as e:
        raise PermanentError(f"postgres {type(e).__name__}") from e
