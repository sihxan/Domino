"""domino_core.ids: prefixed ULIDs and deterministic ids."""

from __future__ import annotations

import re
from typing import Any

import pytest

from domino_core.errors import PermanentError
from domino_core.ids import det_id, new_id

PREFIXES = ["doc", "sty", "chk", "tri", "ev", "ent", "evt", "src", "sl", "th", "sp", "pred", "rpt", "run", "rq", "tr"]


def test_sixteen_prefixes() -> None:
    assert len(set(PREFIXES)) == 16


@pytest.mark.parametrize("prefix", PREFIXES)
def test_new_id_format(prefix: str) -> None:
    value = new_id(prefix)  # type: ignore[arg-type]
    assert re.fullmatch(rf"{prefix}_[0-9A-HJKMNP-TV-Z]{{26}}", value)


@pytest.mark.parametrize("prefix", ["", "foo", "DOC", "doc_", "event", "entity"])
def test_new_id_rejects_unknown_prefix(prefix: Any) -> None:
    with pytest.raises(PermanentError):
        new_id(prefix)


def test_new_id_sorted_in_generation_order() -> None:
    ids = [new_id("doc") for _ in range(1000)]
    assert len(set(ids)) == len(ids)
    assert ids == sorted(ids)


def test_det_id_is_deterministic() -> None:
    assert det_id("ev", "doc_1", "chk_2", "quote") == det_id("ev", "doc_1", "chk_2", "quote")


def test_det_id_format() -> None:
    value = det_id("evt", "a", "b")
    assert re.fullmatch(r"evt_[0-9a-f]{20}", value)


def test_det_id_matches_spec_formula() -> None:
    import hashlib

    expected = "ev_" + hashlib.sha1(b"a|b|c").hexdigest()[:20]
    assert det_id("ev", "a", "b", "c") == expected


def test_det_id_differs_by_parts() -> None:
    assert det_id("ev", "a", "b") != det_id("ev", "ab")
