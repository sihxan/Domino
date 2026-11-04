"""Prefixed ULID ids and deterministic sha1 ids."""

from __future__ import annotations

import hashlib
from typing import Literal, get_args

from ulid import ULID

from domino_core.errors import PermanentError

Prefix = Literal[
    "doc", "sty", "chk", "tri", "ev", "ent", "evt", "src", "sl", "th", "sp", "pred", "rpt", "run", "rq", "tr"
]
_PREFIXES: frozenset[str] = frozenset(get_args(Prefix))


def new_id(prefix: Prefix) -> str:  # f"{prefix}_{ulid()}"
    if prefix not in _PREFIXES:
        raise PermanentError(f"unknown id prefix: {prefix!r}")
    return f"{prefix}_{ULID()}"


def det_id(prefix: str, *parts: str) -> str:  # f"{prefix}_{sha1('|'.join(parts))[:20]}" — 결정론적 (evidence, event)
    return f"{prefix}_{hashlib.sha1('|'.join(parts).encode()).hexdigest()[:20]}"
