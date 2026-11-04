"""Alembic environment: the DB URL is `Settings().pg_dsn` (SecretStr); schema is raw SQL (no autogenerate)."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from domino_core.settings import Settings

target_metadata = None


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of connecting."""
    context.configure(url=Settings().pg_dsn.get_secret_value(), target_metadata=target_metadata, literal_binds=True)  # type: ignore[call-arg]
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in one transaction on a real connection."""
    engine = create_engine(Settings().pg_dsn.get_secret_value())  # type: ignore[call-arg]
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
