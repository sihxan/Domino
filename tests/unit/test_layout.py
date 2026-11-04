"""Repo layout: every domino_* package is importable."""

from __future__ import annotations

import importlib

import pytest

PACKAGES = [
    "domino_core",
    "domino_ingest",
    "domino_extract",
    "domino_resolve",
    "domino_graph",
    "domino_signals",
    "domino_agent",
    "domino_report",
    "domino_eval",
]


@pytest.mark.parametrize("name", PACKAGES)
def test_packages_importable(name: str) -> None:
    module = importlib.import_module(name)
    assert module.__doc__
