"""Redis client from `settings.redis_url` and error mapping for calls made with it."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import redis

from domino_core.clients import default_settings
from domino_core.errors import PermanentError, TransientError
from domino_core.settings import Settings

_client: redis.Redis | None = None


def get_redis(settings: Settings | None = None) -> redis.Redis:
    """Redis client (str responses) for `settings.redis_url`; with no argument, a process-wide default."""
    global _client
    if settings is None and _client is not None:
        return _client
    s = settings or default_settings()
    client = redis.Redis.from_url(s.redis_url.get_secret_value(), decode_responses=True)
    if settings is None:
        _client = client
    return client


@contextmanager
def redis_errors() -> Iterator[None]:
    """Map redis errors raised in the block: connection·timeout→TransientError, others→PermanentError."""
    try:
        yield
    except (redis.ConnectionError, redis.TimeoutError) as e:
        raise TransientError(f"redis {type(e).__name__}") from e
    except redis.RedisError as e:
        raise PermanentError(f"redis {type(e).__name__}: {e}") from e
