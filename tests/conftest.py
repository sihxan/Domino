"""Shared pytest configuration for unit, integration, and golden tests."""

import os

# Docker Desktop (macOS) reports a socket path under $HOME that cannot be bind-mounted into the Ryuk reaper;
# /var/run/docker.sock is valid inside the VM there and is the default on Linux/CI.
os.environ.setdefault("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock")
