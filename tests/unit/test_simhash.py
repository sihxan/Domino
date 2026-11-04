"""simhash64, hamming, shingles, and the signed/unsigned bigint conversion."""

from __future__ import annotations

import random

import pytest

from domino_core.errors import PermanentError
from domino_ingest.simhash import hamming, shingles, simhash64, to_signed, to_unsigned

# Two disjoint vocabularies so the "unrelated" documents share no shingles.
_ENERGY = [
    "grid",
    "turbine",
    "transformer",
    "substation",
    "megawatt",
    "capacity",
    "outage",
    "utility",
    "voltage",
    "cable",
    "demand",
    "generation",
    "reserve",
    "peak",
    "load",
    "interconnect",
    "curtailment",
    "dispatch",
    "tariff",
    "winter",
    "heat",
    "storage",
]
_FARM = [
    "harvest",
    "wheat",
    "soybean",
    "fertilizer",
    "irrigation",
    "drought",
    "acreage",
    "yield",
    "tractor",
    "silo",
    "exports",
    "planting",
    "rainfall",
    "barley",
    "livestock",
    "seed",
    "pasture",
    "orchard",
    "monsoon",
    "grain",
    "cooperative",
]


def _text(words: list[str], n: int, seed: int) -> str:
    rng = random.Random(seed)
    return " ".join(rng.choice(words) for _ in range(n))


def _sim(text: str) -> int:
    return simhash64(shingles(text))


def test_identical_documents_have_hamming_zero() -> None:
    text = _text(_ENERGY, 300, seed=1)
    assert hamming(_sim(text), _sim(text)) == 0


def test_reprint_with_identical_first_300_chars_is_within_3() -> None:
    lead = _text(_ENERGY, 80, seed=2)[:300]
    original = lead + " " + _text(_ENERGY, 200, seed=3)
    reprint = lead + " " + _text(_ENERGY, 200, seed=4)
    assert original[:300] == reprint[:300]
    assert hamming(_sim(original[:300]), _sim(reprint[:300])) <= 3


def test_unrelated_documents_are_far_apart() -> None:
    a = _sim(_text(_ENERGY, 300, seed=6))
    b = _sim(_text(_FARM, 300, seed=7))
    assert hamming(a, b) > 20


def test_hamming_counts_differing_bits() -> None:
    assert hamming(0, 0) == 0
    assert hamming(0b1011, 0b0010) == 2
    assert hamming(0, 2**64 - 1) == 64


def test_simhash_is_unsigned_64_bit_and_empty_is_zero() -> None:
    assert simhash64([]) == 0
    value = _sim(_text(_ENERGY, 50, seed=8))
    assert 0 <= value < 2**64


def test_simhash_value_is_stable() -> None:
    # Persisted in Postgres: the hash function (blake2b, 8 bytes) must never change silently.
    assert simhash64(["a b c", "b c d", "c d e"]) == simhash64(["c d e", "a b c", "b c d"])
    assert simhash64(["alpha"]) == int.from_bytes(
        __import__("hashlib").blake2b(b"alpha", digest_size=8).digest(), "big"
    )


def test_shingles_are_word_trigrams_lowercased() -> None:
    assert shingles("The Grid  holds, again") == ["the grid holds", "grid holds again"]
    assert shingles("a b c d", n=2) == ["a b", "b c", "c d"]


def test_shingles_short_text_and_empty() -> None:
    assert shingles("only two") == ["only two"]
    assert shingles("") == []
    assert shingles("   ... ,, ") == []


def test_shingles_split_cjk_into_characters() -> None:
    assert shingles("日本銀行") == ["日 本 銀", "本 銀 行"]
    assert shingles("한국은행 기준금리 동결 발표") == ["한국은행 기준금리 동결", "기준금리 동결 발표"]


@pytest.mark.parametrize("value", [0, 1, 2**63 - 1, 2**63, 2**64 - 1, 0xDEADBEEFCAFEBABE])
def test_signed_unsigned_round_trip(value: int) -> None:
    signed = to_signed(value)
    assert -(2**63) <= signed <= 2**63 - 1
    assert to_unsigned(signed) == value


def test_signed_is_twos_complement() -> None:
    assert to_signed(2**63) == -(2**63)
    assert to_signed(2**64 - 1) == -1
    assert to_signed(5) == 5
    assert to_unsigned(-1) == 2**64 - 1


def test_conversion_rejects_out_of_range() -> None:
    with pytest.raises(PermanentError):
        to_signed(2**64)
    with pytest.raises(PermanentError):
        to_signed(-1)
    with pytest.raises(PermanentError):
        to_unsigned(2**63)
    with pytest.raises(PermanentError):
        to_unsigned(-(2**63) - 1)
