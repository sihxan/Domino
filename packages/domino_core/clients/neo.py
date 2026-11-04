"""Neo4j 5 driver/session with error mapping, and `run_cypher_file` for `;`-separated DDL files."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import neo4j.exceptions as neo4j_exc
from neo4j import Driver, GraphDatabase, Session

from domino_core.clients import default_settings
from domino_core.errors import PermanentError, TransientError
from domino_core.settings import Settings

_driver: Driver | None = None


def get_driver(settings: Settings | None = None) -> Driver:
    """Neo4j driver for `settings.neo4j_uri`; with no argument, a process-wide default."""
    global _driver
    if settings is None and _driver is not None:
        return _driver
    s = settings or default_settings()
    driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password.get_secret_value()))
    if settings is None:
        _driver = driver
    return driver


@contextmanager
def session(*, driver: Driver | None = None) -> Iterator[Session]:
    """Neo4j session; driver errors inside the block become TransientError/PermanentError."""
    try:
        with (driver or get_driver()).session() as s:
            yield s
    except (neo4j_exc.ServiceUnavailable, neo4j_exc.SessionExpired, neo4j_exc.TransientError) as e:
        raise TransientError(f"neo4j {type(e).__name__}") from e
    except (neo4j_exc.Neo4jError, neo4j_exc.DriverError) as e:
        raise PermanentError(f"neo4j {type(e).__name__}: {e}") from e


def split_statements(text: str) -> list[str]:
    """Split Cypher on `;`, dropping `//` full-line comments and empty statements (no `;` inside literals)."""
    lines = [line for line in text.splitlines() if not line.lstrip().startswith("//")]
    return [stmt.strip() for stmt in "\n".join(lines).split(";") if stmt.strip()]


def run_cypher_file(path: str | Path, *, driver: Driver | None = None) -> int:
    """Run each `;`-separated statement of `path` in its own auto-commit transaction; returns the count."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise PermanentError(f"cypher file unreadable: {path}") from e
    statements = split_statements(text)
    with session(driver=driver) as s:
        for stmt in statements:
            s.run(stmt).consume()
    return len(statements)
