"""64-bit SimHash (Charikar, blake2b 8-byte feature hash), word shingles, and signed/unsigned bigint conversion."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

from domino_core.errors import PermanentError

_BITS = 64
_MASK = (1 << _BITS) - 1
_SIGN = 1 << (_BITS - 1)

# CJK ideographs and kana have no word spaces: each character is its own token. Everything else is a \w+ word.
_CJK = "぀-ヿ㐀-䶿一-鿿"
_TOKEN = re.compile(rf"[{_CJK}]|[^\W{_CJK}]+")


def shingles(text: str, n: int = 3) -> list[str]:
    """Overlapping word n-grams of the lowercased text, each joined by a space; fewer than n tokens → one shingle."""
    tokens = _TOKEN.findall(text.lower())
    if not tokens:
        return []
    if len(tokens) <= n:
        return [" ".join(tokens)]
    return [" ".join(tokens[i : i + n]) for i in range(len(tokens) - n + 1)]


def simhash64(tokens: Iterable[str]) -> int:
    """Charikar SimHash of `tokens` as an unsigned 64-bit int (0 for no tokens)."""
    counts = [0] * _BITS
    seen = False
    for token in tokens:
        seen = True
        h = int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest(), "big")
        for bit in range(_BITS):
            counts[bit] += 1 if (h >> bit) & 1 else -1
    if not seen:
        return 0
    return sum(1 << bit for bit in range(_BITS) if counts[bit] > 0)


def hamming(a: int, b: int) -> int:
    """Number of differing bits between two 64-bit hashes."""
    return ((a ^ b) & _MASK).bit_count()


def to_signed(value: int) -> int:
    """Unsigned 64-bit → two's complement signed, for the Postgres `bigint` column."""
    if not 0 <= value <= _MASK:
        raise PermanentError(f"simhash out of unsigned 64-bit range: {value}")
    return value - (1 << _BITS) if value & _SIGN else value


def to_unsigned(value: int) -> int:
    """Signed `bigint` read from Postgres → unsigned 64-bit simhash."""
    if not -_SIGN <= value < _SIGN:
        raise PermanentError(f"simhash out of signed 64-bit range: {value}")
    return value & _MASK
