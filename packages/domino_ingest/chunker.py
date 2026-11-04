"""Chunker: paragraph, then sentence, then token-cap splitting with offsets in the source document text."""

from __future__ import annotations

import contextlib
import io
import os
import re
from collections.abc import Callable
from functools import cache, lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any

import tiktoken

from domino_core.errors import PermanentError, TransientError
from domino_core.ids import new_id
from domino_core.log import get_logger
from domino_core.models import Chunk, Document

log = get_logger(__name__)

ENCODING_NAME = "cl100k_base"
MIN_MAX_TOKENS = 8
_KSS_BACKEND = "fast"

Span = tuple[int, int]  # [start, end) in doc.text, never starting or ending on whitespace

_PARAGRAPH_BREAK = re.compile(r"\n[^\S\n]*\n\s*")
# Sentence end (ASCII/ellipsis then whitespace, or CJK terminator) plus any closing quotes/brackets.
_SENTENCE_END = re.compile(r"""(?:[.!?…]+["'”’)\]]*\s+|[。！？]+[」』”’)\]]*\s*)""")  # noqa: RUF001


# --- tokens ---------------------------------------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    """cl100k_base, downloaded by tiktoken on first use into a stable cache dir (TIKTOKEN_CACHE_DIR wins)."""
    if "TIKTOKEN_CACHE_DIR" not in os.environ and "DATA_GYM_CACHE_DIR" not in os.environ:
        cache = Path.home() / ".cache" / "domino" / "tiktoken"
        cache.mkdir(parents=True, exist_ok=True)
        os.environ["TIKTOKEN_CACHE_DIR"] = str(cache)
    try:
        return tiktoken.get_encoding(ENCODING_NAME)
    except Exception as exc:  # download failure surfaces as requests/OSError subclasses
        raise TransientError(f"tiktoken {ENCODING_NAME} unavailable (offline and not cached?): {exc}") from exc


def count_tokens(text: str) -> int:
    """Token count under cl100k_base; special-token lookalikes in the text are counted as plain text."""
    return len(_encoding().encode_ordinary(text))


# --- sentence splitting ---------------------------------------------------------------------------------------------


class _SplitMismatch(Exception):
    """A sentence splitter returned text that cannot be located in the source."""


@lru_cache(maxsize=1)
def _load_kss() -> Callable[..., list[str]]:
    # kss prints an install hint for mecab on import; keep stdout/stderr clean for CLI callers.
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        import kss

        split: Callable[..., list[str]] = kss.split_sentences
        split("워밍업 문장이다. 두 번째 문장이다.", backend=_KSS_BACKEND, num_workers=1)
    return split


def _kss_split(text: str) -> list[str]:
    # "fast" is rule-based (~30 ms per 3,000 chars); the default morpheme backend (pecab) took ~35 s for the same text.
    return _load_kss()(text, backend=_KSS_BACKEND, num_workers=1)


@cache
def _pysbd_segmenter(lang: str) -> Any:
    import pysbd

    try:
        return pysbd.Segmenter(language=lang, clean=False, char_span=True)
    except ValueError:  # language unsupported by pysbd
        return pysbd.Segmenter(language="en", clean=False, char_span=True)


def _starts_from_sentences(para: str, sentences: list[str]) -> list[int]:
    starts: list[int] = []
    cursor = 0
    for sent in sentences:
        if not sent.strip():
            continue
        idx = para.find(sent, cursor)
        if idx < 0:
            raise _SplitMismatch(sent[:40])
        starts.append(idx)
        cursor = idx + len(sent)
    return starts


def _sentence_starts(para: str, lang: str) -> list[int]:
    """Offsets in `para` where sentences start. Splitter output is only trusted if it maps back onto `para`."""
    try:
        if lang == "ko":
            return _starts_from_sentences(para, _kss_split(para))
        spans = _pysbd_segmenter(lang).segment(para)
        for span in spans:
            if para[span.start : span.end] != span.sent:
                raise _SplitMismatch(span.sent[:40])
        return [span.start for span in spans if span.sent.strip()]
    except _SplitMismatch as exc:
        log.warning("chunker.splitter_mismatch_fallback", lang=lang, sample=str(exc))
    except Exception as exc:  # splitter crashed on odd input: regex split keeps the pipeline moving
        log.warning("chunker.splitter_error_fallback", lang=lang, error=repr(exc))
    return [0, *(m.end() for m in _SENTENCE_END.finditer(para))]


def _trimmed(text: str, start: int, end: int) -> Span | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _sentence_spans(text: str, span: Span, lang: str) -> list[Span]:
    """Sentences of one paragraph in document coordinates; together they cover the whole paragraph."""
    para_start, para_end = span
    starts = sorted({0, *_sentence_starts(text[para_start:para_end], lang)})
    bounds = [para_start + s for s in starts] + [para_end]
    spans = (_trimmed(text, a, b) for a, b in pairwise(bounds))
    return [s for s in spans if s]


def _paragraph_spans(text: str) -> list[Span]:
    spans: list[Span] = []
    pos = 0
    for m in (*_PARAGRAPH_BREAK.finditer(text), None):
        end = m.start() if m else len(text)
        if (s := _trimmed(text, pos, end)) is not None:
            spans.append(s)
        pos = m.end() if m else len(text)
    return spans


# --- forced split ---------------------------------------------------------------------------------------------------


def _split_by_tokens(text: str, span: Span, max_tokens: int) -> list[Span]:
    """Split one over-long sentence into pieces of at most max_tokens, preferring whitespace cut points."""
    start, end = span
    pieces: list[Span] = []
    while start < end:
        if count_tokens(text[start:end]) <= max_tokens:
            pieces.append((start, end))
            break
        # Largest cut with <= max_tokens: grow an upper bound by doubling, then bisect.
        lo, hi = start + 1, min(end - 1, start + max_tokens * 4)
        while hi < end - 1 and count_tokens(text[start:hi]) <= max_tokens:
            lo, hi = hi, min(end - 1, start + (hi - start) * 2)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if count_tokens(text[start:mid]) <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        cut = lo
        for i in range(cut - 1, start + (cut - start) // 2, -1):  # back off to whitespace in the second half
            if text[i].isspace():
                cut = i
                break
        if (piece := _trimmed(text, start, cut)) is not None:
            pieces.append(piece)
        start = cut
        while start < end and text[start].isspace():
            start += 1
    return pieces


# --- chunking -------------------------------------------------------------------------------------------------------


def chunk(doc: Document, *, max_tokens: int = 512, overlap: int = 64) -> list[Chunk]:
    """Split doc.text into chunks of <= max_tokens: paragraphs first, then sentences, then tokens (offsets in doc.text).

    A paragraph that fits in max_tokens is never split. An over-long paragraph is cut at sentence boundaries, and a
    sentence over max_tokens is cut by tokens. Each chunk after the first starts with the trailing whole sentences of
    the previous chunk whose token count is closest to `overlap` (never the whole previous chunk), so Korean text where
    one sentence exceeds `overlap` tokens still gets one sentence of context.
    """
    if max_tokens < MIN_MAX_TOKENS:
        raise PermanentError(f"max_tokens must be >= {MIN_MAX_TOKENS}, got {max_tokens}")
    if not 0 <= overlap < max_tokens:
        raise PermanentError(f"overlap must satisfy 0 <= overlap < max_tokens, got {overlap}")

    text = doc.text
    memo: dict[Span, int] = {}

    def tokens(start: int, end: int) -> int:
        if (start, end) not in memo:
            memo[(start, end)] = count_tokens(text[start:end])
        return memo[(start, end)]

    def fits(units: list[Span]) -> bool:
        return tokens(units[0][0], units[-1][1]) <= max_tokens

    def tail(units: list[Span]) -> list[Span]:
        """Trailing whole sentences whose token count is closest to `overlap` (ties: fewer); never the first unit."""
        best, best_gap = 0, overlap  # number of trailing units kept, |tokens - overlap|
        for k in range(1, len(units)):
            n = tokens(units[-k][0], units[-1][1])
            if (gap := abs(n - overlap)) < best_gap:
                best, best_gap = k, gap
            elif n > 2 * overlap:
                break
        return units[len(units) - best :]

    chunks: list[Chunk] = []
    cur: list[Span] = []
    n_new = 0  # units in `cur` that are not carried-over overlap

    def emit() -> None:
        nonlocal cur, n_new
        start, end = cur[0][0], cur[-1][1]
        chunks.append(
            Chunk(
                id=new_id("chk"),
                doc_id=doc.id,
                idx=len(chunks),
                char_start=start,
                char_end=end,
                text=text[start:end],
                n_tokens=tokens(start, end),
            )
        )
        cur, n_new = tail(cur), 0

    for para in _paragraph_spans(text):
        if tokens(*para) <= max_tokens:
            groups = [[para]]  # atomic: a paragraph that fits is never split
        else:
            if n_new:  # an over-long paragraph starts on a fresh chunk
                emit()
            sentences = _sentence_spans(text, para, doc.lang)
            groups = [
                [piece]
                for sent in sentences
                for piece in ([sent] if tokens(*sent) <= max_tokens else _split_by_tokens(text, sent, max_tokens))
            ]
        for group in groups:
            while cur and not fits([*cur, *group]):
                if n_new:
                    emit()
                else:
                    cur = cur[1:]  # drop carried-over overlap to make room
            cur.extend(group)
            n_new += len(group)
    if n_new:
        emit()
    return chunks
