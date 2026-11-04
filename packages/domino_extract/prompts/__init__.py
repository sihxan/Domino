"""Prompt texts for extraction, loaded verbatim from the .md files next to this module."""

from __future__ import annotations

from pathlib import Path

_DIR = Path(__file__).parent


def load_prompt(name: str) -> str:
    """Read `<name>` from this package's directory as UTF-8, unchanged (the system prompt is a cache key)."""
    return (_DIR / name).read_text(encoding="utf-8")


EXTRACT_SYSTEM = load_prompt("extract_system.md")
