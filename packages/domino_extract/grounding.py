"""Grounding: locate an LLM quote in chunk text and return its span in the *source* (pre-normalization) text."""

from __future__ import annotations

import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache

from rapidfuzz import fuzz

from domino_core.log import get_logger

log = get_logger(__name__)

FUZZY_MIN_SCORE = 95.0  # rapidfuzz partial_ratio threshold for the re-search (tech §5.1 extract.validate (1))

_BOM = chr(0xFEFF)
# Typographic quotes -> ASCII, applied only to the matching key (never to `norm`): models routinely straighten them.
_QUOTE_FOLD = str.maketrans(
    {
        0x2018: "'",
        0x2019: "'",
        0x201A: "'",
        0x201B: "'",
        0x201C: '"',
        0x201D: '"',
        0x201E: '"',
        0x201F: '"',
    }
)


@dataclass(frozen=True)
class _Normalized:
    """Normalized text plus, for each of its characters, the source span [start, end) it came from."""

    text: str
    starts: tuple[int, ...]
    ends: tuple[int, ...]


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def _clusters(s: str) -> Iterator[tuple[int, int, str]]:
    """Yield (start, end, nfkc) runs of `s` whose NFKC form does not depend on neighbouring runs.

    NFKC is not per-character: combining marks and Hangul jamo compose with what precedes them, and compatibility
    characters expand. A run is closed only when appending the next character leaves the NFKC of the run unchanged
    and that character's own NFKC form starts with a starter (a character that decomposes into combining marks, such
    as U+0F73, reorders with the marks before it even though its own combining class is 0). So the run's
    normalization can be attributed to exactly that source slice.
    """
    if s.isascii():  # NFKC is the identity on ASCII
        for i, ch in enumerate(s):
            yield i, i + 1, ch
        return
    start = 0
    cur = _nfkc(s[0:1])
    for i in range(1, len(s)):
        piece = _nfkc(s[i])
        joined = _nfkc(s[start : i + 1])
        if unicodedata.combining(piece[0]) == 0 and joined == cur + piece:
            yield start, i, cur
            start = i
            cur = piece
        else:
            cur = joined
    if s:
        yield start, len(s), cur


def _normalize(s: str, *, fold_quotes: bool = False) -> _Normalized:
    """`norm` with an offset table: NFKC, delete U+FEFF, collapse whitespace runs to one space, strip."""
    clusters = list(_clusters(s))
    if "".join(c[2] for c in clusters) != _nfkc(s):  # defensive: cluster boundaries were not stable
        log.warning("grounding.nfkc_cluster_mismatch", length=len(s))
        clusters = [(i, i + 1, ch) for i, ch in enumerate(s)]  # skip NFKC; offsets stay exact
    chars: list[str] = []
    starts: list[int] = []
    ends: list[int] = []
    ws: tuple[int, int] | None = None  # source span of the whitespace run being collapsed
    for a, b, n in clusters:
        for ch in n:
            if ch == _BOM:
                continue
            if ch.isspace():  # same set as re's \s for str patterns (tested)
                ws = (a, b) if ws is None else (ws[0], b)
                continue
            if ws is not None:
                if chars:  # leading whitespace is stripped, not collapsed
                    chars.append(" ")
                    starts.append(ws[0])
                    ends.append(ws[1])
                ws = None
            chars.append(ch.translate(_QUOTE_FOLD) if fold_quotes else ch)
            starts.append(a)
            ends.append(b)
    return _Normalized("".join(chars), tuple(starts), tuple(ends))  # a trailing whitespace run is stripped


@lru_cache(maxsize=32)
def _normalize_folded(s: str) -> _Normalized:
    """Matching-key normalization (quotes folded); cached because one chunk text is grounded once per triple."""
    return _normalize(s, fold_quotes=True)


def norm(s: str) -> str:
    """NFKC + whitespace-run collapse + strip (code §2.4); also deletes U+FEFF (BOM), which NFKC and `\\s` keep."""
    return _normalize(s).text


def _source_span(n: _Normalized, i: int, j: int) -> tuple[int, int] | None:
    """Map the normalized window [i, j) back to source offsets, trimmed so it starts and ends on a visible char."""
    while i < j and n.text[i] == " ":
        i += 1
    while j > i and n.text[j - 1] == " ":
        j -= 1
    if i >= j:
        return None
    return n.starts[i], n.ends[j - 1]


def ground(quote: str, text: str) -> tuple[int, int] | None:
    """Return (start, end) of `quote` in `text` as offsets into the original `text` (so `text[start:end]` is the span).

    Both strings are normalized (NFKC, BOM removed, whitespace collapsed, typographic quotes folded) and searched with
    `str.find`; on failure `rapidfuzz.fuzz.partial_ratio_alignment` re-searches with a score cutoff of 95. A match that
    begins or ends inside a character that normalization expanded or merged is widened to that whole source character
    run. Returns None when the normalized quote is empty or longer than the text, or when nothing scores >= 95.
    """
    q = _normalize_folded(quote).text
    if not q:
        return None
    t = _normalize_folded(text)
    i = t.text.find(q)
    if i >= 0:
        return _source_span(t, i, i + len(q))
    if len(q) > len(t.text):  # partial_ratio would align the whole text against part of the quote: not a quote of it
        return None
    alignment = fuzz.partial_ratio_alignment(q, t.text, score_cutoff=FUZZY_MIN_SCORE)
    if alignment is None:
        return None
    return _source_span(t, alignment.dest_start, alignment.dest_end)
