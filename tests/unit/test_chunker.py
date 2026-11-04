"""Chunker: paragraph-first boundaries, token cap, sentence overlap, source-text offsets."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from itertools import pairwise

import pytest
import tiktoken

from domino_core.errors import PermanentError, TransientError
from domino_core.models import Chunk, Document
from domino_ingest import chunker
from domino_ingest.chunker import chunk, count_tokens

MAX = 512
OVERLAP = 64


@pytest.fixture(autouse=True, scope="module")
def _encoding_available() -> Iterator[None]:
    """tiktoken downloads cl100k_base on first use; DOMINO_TEST_OFFLINE=1 skips these tests when it is not cached."""
    try:
        count_tokens("warm up")
    except TransientError:
        if os.environ.get("DOMINO_TEST_OFFLINE") == "1":
            pytest.skip("cl100k_base not cached and DOMINO_TEST_OFFLINE=1")
        raise
    yield


def make_doc(text: str, lang: str = "en") -> Document:
    return Document(
        id="doc_01TEST",
        source_id="src_test",
        url="https://example.com/a",
        url_hash="h",
        title=None,
        published_at=None,
        lang=lang,
        doc_type="NEWS",
        text=text,
        text_sha="s",
        simhash=0,
        snapshot_key="raw/2026/11/doc.html",
        snapshot_sha="s",
    )


# --- fixtures -------------------------------------------------------------------------------------------------------


def en_sentence(i: int) -> str:
    return (
        f"Industrial output in district {i} rose {i % 7 + 1}.{i % 10} percent in the third quarter, "
        f"the statistics office said, as exports of machinery and electronic parts recovered."
    )


def ko_sentence(i: int) -> str:
    return (
        f"{i}번 지역의 3분기 산업생산은 전년 동기보다 {i % 7 + 1}.{i % 10}퍼센트 늘었다고 통계청이 밝혔으며, "
        f"기계와 전자부품 수출이 회복된 영향이 컸다."
    )


def en_paragraphs() -> list[str]:
    short1 = "The statistics office published its quarterly report on Tuesday. Output rose in most districts."
    long_para = " ".join(en_sentence(i) for i in range(1, 36))  # ~1000 tokens: must split on sentence boundaries
    short2 = "Analysts noted that the pace of growth was broadly in line with the central bank projection."
    giant = (
        ", and ".join(  # one sentence, no terminal punctuation until the end: forced token split
            f"the survey found that firms in district {i} reported higher input costs and longer delivery times"
            for i in range(1, 60)
        )
        + "."
    )
    short3 = "The next release is scheduled for January."
    return [short1, long_para, short2, giant, short3]


def ko_paragraphs() -> list[str]:
    short1 = "통계청은 화요일에 분기 보고서를 발표했다. 대부분의 지역에서 생산이 늘었다."
    long_para = " ".join(ko_sentence(i) for i in range(1, 40))
    short2 = "전문가들은 성장 속도가 중앙은행 전망과 대체로 일치한다고 평가했다."
    clauses = (f"{i}번 지역 기업들은 투입 비용 상승과 납품 지연을 함께 보고했다는 결과가 나왔고" for i in range(1, 60))
    giant = ", 그리고 ".join(clauses) + "."
    short3 = "다음 발표는 1월로 예정되어 있다."
    return [short1, long_para, short2, giant, short3]


@pytest.fixture(params=["en", "ko"])
def lang_doc(request: pytest.FixtureRequest) -> tuple[Document, list[str]]:
    lang = request.param
    paras = en_paragraphs() if lang == "en" else ko_paragraphs()
    return make_doc("\n\n".join(paras), lang), paras


def para_spans(text: str, paras: list[str]) -> list[tuple[int, int]]:
    spans, pos = [], 0
    for p in paras:
        s = text.index(p, pos)
        spans.append((s, s + len(p)))
        pos = s + len(p)
    return spans


def overlap_tokens(prev: Chunk, nxt: Chunk, text: str) -> int:
    if nxt.char_start >= prev.char_end:
        return 0
    return count_tokens(text[nxt.char_start : prev.char_end])


# --- tests ----------------------------------------------------------------------------------------------------------


def test_empty_and_whitespace_documents() -> None:
    assert chunk(make_doc("")) == []
    assert chunk(make_doc("  \n\n \t\n")) == []


def test_short_document_is_single_chunk() -> None:
    doc = make_doc("  Rates were unchanged.  \n")
    [c] = chunk(doc)
    assert (c.idx, c.doc_id) == (0, "doc_01TEST")
    assert c.text == "Rates were unchanged."
    assert doc.text[c.char_start : c.char_end] == c.text
    assert c.n_tokens == count_tokens(c.text)
    assert c.id.startswith("chk_")


def test_offsets_ids_and_counts(lang_doc: tuple[Document, list[str]]) -> None:
    doc, _ = lang_doc
    chunks = chunk(doc)
    assert len(chunks) > 3
    assert [c.idx for c in chunks] == list(range(len(chunks)))
    assert len({c.id for c in chunks}) == len(chunks)
    for c in chunks:
        assert c.id.startswith("chk_")
        assert c.doc_id == doc.id
        assert doc.text[c.char_start : c.char_end] == c.text
        assert c.n_tokens == count_tokens(c.text)
        assert c.text == c.text.strip()
    assert all(a.char_start < b.char_start and a.char_end < b.char_end for a, b in pairwise(chunks))


def test_every_chunk_within_token_cap(lang_doc: tuple[Document, list[str]]) -> None:
    doc, _ = lang_doc
    assert all(c.n_tokens <= MAX for c in chunk(doc))
    small = chunk(doc, max_tokens=96, overlap=16)
    assert all(c.n_tokens <= 96 for c in small)
    assert all(doc.text[c.char_start : c.char_end] == c.text for c in small)


def test_no_text_is_lost(lang_doc: tuple[Document, list[str]]) -> None:
    doc, _ = lang_doc
    covered = bytearray(len(doc.text))
    for c in chunk(doc):
        covered[c.char_start : c.char_end] = b"\x01" * (c.char_end - c.char_start)
    missing = [i for i, ch in enumerate(doc.text) if not ch.isspace() and not covered[i]]
    assert missing == []


def test_paragraph_boundaries_come_first(lang_doc: tuple[Document, list[str]]) -> None:
    doc, paras = lang_doc
    spans = para_spans(doc.text, paras)
    chunks = chunk(doc)
    # The two short leading paragraphs fit together, the long paragraph does not fit after them: the first chunk
    # ends exactly at the end of paragraph 0 (not mid-sentence of paragraph 1), and no short paragraph is ever cut.
    assert chunks[0].char_end == spans[0][1]
    for s, e in (spans[0], spans[2], spans[4]):
        containing = [c for c in chunks if c.char_start <= s and c.char_end >= e]
        assert containing, f"short paragraph {doc.text[s:e]!r} was split across chunks"
    # Every chunk's new content starts on a sentence/paragraph start: chunk starts are never mid-word.
    for c in chunks:
        assert c.char_start == 0 or doc.text[c.char_start - 1].isspace()


def test_short_paragraphs_pack_into_one_chunk() -> None:
    paras = ["First short paragraph about trade.", "Second short paragraph about energy.", "Third one on policy."]
    doc = make_doc("\n\n".join(paras))
    [c] = chunk(doc)
    assert c.text == doc.text
    assert c.n_tokens < 60


def test_adjacent_chunks_overlap_by_about_64_tokens(lang_doc: tuple[Document, list[str]]) -> None:
    doc, paras = lang_doc
    spans = para_spans(doc.text, paras)
    chunks = chunk(doc)
    inside_long = [(a, b) for a, b in pairwise(chunks) if a.char_start >= spans[1][0] and b.char_end <= spans[1][1]]
    assert inside_long, "fixture must produce adjacent chunks inside the long paragraph"
    for a, b in inside_long:
        n = overlap_tokens(a, b, doc.text)
        assert OVERLAP // 2 <= n <= OVERLAP * 3 // 2, n
    # Whole sentences closest to the target never overshoot twice the target, including paragraph/forced-split edges.
    for a, b in pairwise(chunks):
        assert overlap_tokens(a, b, doc.text) <= 2 * OVERLAP


def test_overlap_is_whole_sentences() -> None:
    sents = [en_sentence(i) for i in range(1, 16)]
    doc = make_doc(" ".join(sents))
    chunks = chunk(doc, max_tokens=160, overlap=48)
    assert len(chunks) > 2
    for a, b in pairwise(chunks):
        assert b.text.startswith(doc.text[b.char_start : b.char_start + 20])
        # next chunk starts at a sentence start and the previous chunk ends at a sentence end
        assert doc.text[b.char_start : b.char_start + 30] in {s[:30] for s in sents}
        assert a.text.endswith(".")


def test_oversize_sentence_is_split_by_tokens_with_source_offsets(lang_doc: tuple[Document, list[str]]) -> None:
    doc, paras = lang_doc
    s, e = para_spans(doc.text, paras)[3]
    assert count_tokens(doc.text[s:e]) > MAX
    # the last piece of the sentence may share a chunk with the next paragraph
    touching = [c for c in chunk(doc) if c.char_start < e and c.char_end > s]
    assert len(touching) >= 3
    assert all(c.n_tokens <= MAX for c in touching)
    assert all(doc.text[c.char_start : c.char_end] == c.text for c in touching)
    assert touching[0].char_start <= s
    assert touching[-1].char_end >= e
    assert [c.char_end for c in touching[:-1]] == sorted(c.char_end for c in touching[:-1])
    for a, b in pairwise(touching):
        assert b.char_start <= a.char_end + 1  # contiguous coverage of the giant sentence


def test_no_whitespace_text_is_split_by_characters() -> None:
    text = "가" * 3000
    doc = make_doc(text, "ko")
    chunks = chunk(doc)
    assert len(chunks) > 1
    assert all(c.n_tokens <= MAX for c in chunks)
    assert all(text[c.char_start : c.char_end] == c.text for c in chunks)
    assert chunks[0].char_start == 0 and chunks[-1].char_end == len(text)


def test_offsets_survive_irregular_whitespace() -> None:
    text = (
        "﻿  Prices fell.\r\n\r\nThe   office\nsaid  inputs   eased.　　Wages rose slightly. "
        + " ".join(en_sentence(i) for i in range(1, 30))
        + "\n\n\n\n  Final paragraph \U0001f4c8 with emoji and 한글 mixed in.  \n"
    )
    doc = make_doc(text)
    chunks = chunk(doc)
    assert len(chunks) >= 2
    assert all(text[c.char_start : c.char_end] == c.text for c in chunks)
    assert all(c.n_tokens <= MAX for c in chunks)


def test_korean_irregular_spacing_keeps_source_offsets() -> None:
    head = "한국은행은   기준금리를 동결했다.  이는\n물가 안정을 고려한 결정이다!\n\n"
    text = head + " ".join(ko_sentence(i) for i in range(1, 30))
    doc = make_doc(text, "ko")
    chunks = chunk(doc)
    assert len(chunks) >= 2
    assert all(text[c.char_start : c.char_end] == c.text for c in chunks)


def test_splitter_that_rewrites_text_falls_back_to_source_offsets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chunker, "_kss_split", lambda s: [" ".join(x.split()).upper() for x in s.split(". ")])
    text = "\n\n".join(" ".join(ko_sentence(i) for i in range(k, k + 8)) for k in (1, 9, 17))
    doc = make_doc(text, "ko")
    chunks = chunk(doc, max_tokens=200, overlap=32)
    assert len(chunks) >= 3
    assert all(text[c.char_start : c.char_end] == c.text for c in chunks)
    assert all(c.n_tokens <= 200 for c in chunks)


@pytest.mark.parametrize("lang", ["ja", "de", "zh", "sv"])
def test_other_languages_use_a_sentence_splitter_and_keep_offsets(lang: str) -> None:
    sample = {
        "ja": "日本銀行は金融政策を維持した。物価の見通しは変わらない。",
        "de": "Die Bundesbank hielt die Prognose unverändert. Die Inflation sank im dritten Quartal auf 2,1 Prozent.",
        "zh": "央行宣布维持利率不变。通胀预期保持稳定。",
        "sv": "Riksbanken lämnade styrräntan oförändrad. Inflationen föll under tredje kvartalet.",
    }[lang]
    text = " ".join([sample] * 40)
    doc = make_doc(text, lang)
    chunks = chunk(doc, max_tokens=128, overlap=24)
    assert len(chunks) > 1
    assert all(text[c.char_start : c.char_end] == c.text for c in chunks)
    assert all(c.n_tokens <= 128 for c in chunks)


def test_n_tokens_matches_tiktoken_cl100k() -> None:
    import tiktoken

    enc = tiktoken.get_encoding("cl100k_base")
    doc = make_doc(en_sentence(3) + " " + ko_sentence(4))
    [c] = chunk(doc)
    assert c.n_tokens == len(enc.encode_ordinary(c.text))


def test_special_token_text_is_counted_not_rejected() -> None:
    doc = make_doc("The log line contained <|endoftext|> literally. It was not a control token.")
    [c] = chunk(doc)
    assert c.n_tokens > 5


def test_is_deterministic_apart_from_ids(lang_doc: tuple[Document, list[str]]) -> None:
    doc, _ = lang_doc

    def key(cs: list[Chunk]) -> list[tuple[int, int, int, str, int]]:
        return [(c.idx, c.char_start, c.char_end, c.text, c.n_tokens) for c in cs]

    assert key(chunk(doc)) == key(chunk(doc))


@pytest.mark.parametrize(("max_tokens", "overlap"), [(4, 0), (512, -1), (512, 512), (64, 100)])
def test_invalid_parameters_raise_permanent_error(max_tokens: int, overlap: int) -> None:
    with pytest.raises(PermanentError):
        chunk(make_doc("Some text."), max_tokens=max_tokens, overlap=overlap)


def test_zero_overlap_chunks_do_not_share_text() -> None:
    doc = make_doc(" ".join(en_sentence(i) for i in range(1, 30)))
    chunks = chunk(doc, max_tokens=200, overlap=0)
    assert len(chunks) > 2
    assert all(a.char_end <= b.char_start for a, b in pairwise(chunks))


def test_fixtures_contain_no_investment_advice_wording() -> None:
    banned = re.compile(r"매수|매도|추천|목표가|확실히|\b(buy|sell|recommend)\w*", re.IGNORECASE)
    assert not banned.search("\n\n".join(en_paragraphs() + ko_paragraphs()))


def test_encoding_download_failure_is_transient(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(name: str) -> None:
        raise OSError("network unreachable")

    chunker._encoding.cache_clear()
    monkeypatch.setattr(tiktoken, "get_encoding", boom)
    try:
        with pytest.raises(TransientError):
            count_tokens("anything")
    finally:
        monkeypatch.undo()
        chunker._encoding.cache_clear()
