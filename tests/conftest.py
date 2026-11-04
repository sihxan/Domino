"""Shared pytest configuration for unit, integration, and golden tests."""

import os
from collections.abc import Iterator

import pytest

from domino_core.settings import get_settings

# Docker Desktop (macOS) reports a socket path under $HOME that cannot be bind-mounted into the Ryuk reaper;
# /var/run/docker.sock is valid inside the VM there and is the default on Linux/CI.
os.environ.setdefault("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock")


@pytest.fixture(autouse=True)
def fresh_settings_cache() -> Iterator[None]:
    """The process-wide get_settings() cache must not leak environment between tests."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
