"""Unit tests for domino_extract.validator: grounding, self-reference, hedge demotion, attribution, drop-rate gate."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from domino_core.errors import PermanentError
from domino_core.models import Chunk, Document, SourceSpec
from domino_extract.schema import CausalTriple, ExtractionResult
from domino_extract.validator import ValidatedTriple, looks_attributive, validate

NBSP = "\u00a0"

# --- fixtures ---------------------------------------------------------------------------------------------------

PREFIX = "Semiconductor supply update.\n\n"  # the chunk does not start at document offset 0
EN_CHUNK_TEXT = (
    "The foundry said wafer shortages push  lead times higher for automotive chips. "
    "Analysts note that rising power costs may squeeze margins at memory makers."
)
KO_CHUNK_TEXT = (
    "한국은행은 환율 상승이 수입물가를 끌어올렸다고 밝혔다. 반도체 수출 증가가 설비투자 확대로 이어질 전망이다."
)


def make_source(**kw: Any) -> SourceSpec:
    base: dict[str, Any] = {
        "id": "src_t_a",
        "country": "US",
        "region": "NA",
        "publisher": "Test Publisher",
        "grade": "A",
        "kind": "rss",
        "url": "https://example.test/feed.xml",
        "parser": "RssCrawler",
        "lang": "en",
    }
    return SourceSpec(**(base | kw))


def make_doc(text: str, lang: str = "en") -> Document:
    return Document(
        id="doc_1",
        source_id="src_t_a",
        url="https://example.test/a",
        url_hash="0" * 64,
        title=None,
        published_at=datetime(2026, 11, 3, tzinfo=UTC),
        lang=lang,
        doc_type="NEWS",
        text=text,
        text_sha="0" * 64,
        simhash=0,
        snapshot_key="snap/a.html",
        snapshot_sha="0" * 64,
    )


def make_chunk(doc: Document, chunk_text: str, *, chunk_id: str = "chk_1") -> Chunk:
    """A chunk whose coordinates really point into `doc.text`."""
    start = doc.text.index(chunk_text)
    return Chunk(
        id=chunk_id,
        doc_id=doc.id,
        idx=1,
        char_start=start,
        char_end=start + len(chunk_text),
        text=chunk_text,
        n_tokens=64,
    )


def en_doc_and_chunk() -> tuple[Document, Chunk]:
    doc = make_doc(PREFIX + EN_CHUNK_TEXT)
    return doc, make_chunk(doc, EN_CHUNK_TEXT)


def make_entity(name: str, **kw: Any) -> dict[str, Any]:
    return {"name": name, "type": "INDICATOR", "direction": "UP", "region": "GL"} | kw


def make_triple(**kw: Any) -> CausalTriple:
    base: dict[str, Any] = {
        "cause": make_entity("wafer shortages"),
        "effect": make_entity("lead times"),
        "mechanism": "shortages lengthen delivery",
        "targets": [],
        "relation_kind": "CAUSAL",
        "causal_type": "DIRECT",
        "polarity": "POSITIVE",
        "horizon": "short",
        "lag_days": None,
        "impact": 0.6,
        "confidence": 0.7,
        "claim_type": "fact",
        "chain_position": None,
        "valid_at_hint": None,
        "quote": "wafer shortages push lead times higher",
    }
    return CausalTriple.model_validate(base | kw)


def make_result(*triples: CausalTriple) -> ExtractionResult:
    return ExtractionResult(event=None, triples=list(triples), no_causal_content=not triples)


def one(res: ExtractionResult, chunk: Chunk, src: SourceSpec) -> ValidatedTriple:
    out, drops = validate(res, chunk, src)
    assert drops == []
    assert len(out) == 1
    return out[0]


# --- hedge demotion ---------------------------------------------------------------------------------------------


def test_hedge_demotes_causal_to_speculative() -> None:
    _, chunk = en_doc_and_chunk()
    t = make_triple(
        cause=make_entity("rising power costs"),
        effect=make_entity("margins"),
        quote="rising power costs may squeeze margins",
    )
    v = one(make_result(t), chunk, make_source())
    assert v.relation_kind == "SPECULATIVE"
    assert v.is_correlation_only is True


def test_causal_without_hedge_stays_causal() -> None:
    _, chunk = en_doc_and_chunk()
    v = one(make_result(make_triple()), chunk, make_source())
    assert v.relation_kind == "CAUSAL"
    assert v.is_correlation_only is False


@pytest.mark.parametrize("kind", ["CORRELATION", "SPECULATIVE", "CONDITIONAL"])
def test_non_causal_kinds_are_kept_and_flagged(kind: str) -> None:
    _, chunk = en_doc_and_chunk()
    v = one(make_result(make_triple(relation_kind=kind)), chunk, make_source())
    assert v.relation_kind == kind  # never upgraded to CAUSAL, never rewritten to SPECULATIVE
    assert v.is_correlation_only is True


def test_hedge_does_not_rewrite_other_kinds() -> None:
    _, chunk = en_doc_and_chunk()
    t = make_triple(
        relation_kind="CORRELATION",
        cause=make_entity("rising power costs"),
        effect=make_entity("margins"),
        quote="rising power costs may squeeze margins",
    )
    assert one(make_result(t), chunk, make_source()).relation_kind == "CORRELATION"


def test_hedge_match_is_case_insensitive() -> None:
    text = "Analysts say rising power costs May squeeze margins at memory makers."
    doc = make_doc(text)
    chunk = make_chunk(doc, text)
    t = make_triple(
        cause=make_entity("rising power costs"),
        effect=make_entity("margins"),
        quote="rising power costs May squeeze margins",
    )
    assert one(make_result(t), chunk, make_source()).relation_kind == "SPECULATIVE"


def test_hedge_matches_through_whitespace_normalization() -> None:
    """A phrase entry ("expected to") must still match when the quote carries a non-breaking space."""
    text = f"Chip output is expected{NBSP}to rise because fabs added lines."
    doc = make_doc(text)
    chunk = make_chunk(doc, text)
    t = make_triple(
        cause=make_entity("fabs added lines"),
        effect=make_entity("chip output"),
        quote=f"Chip output is expected{NBSP}to rise because fabs added lines",
    )
    assert one(make_result(t), chunk, make_source()).relation_kind == "SPECULATIVE"


def test_korean_hedge_demotes_causal() -> None:
    doc = make_doc(PREFIX + KO_CHUNK_TEXT, lang="ko")
    chunk = make_chunk(doc, KO_CHUNK_TEXT)
    t = make_triple(
        cause=make_entity("반도체 수출 증가"),
        effect=make_entity("설비투자 확대"),
        quote="반도체 수출 증가가 설비투자 확대로 이어질 전망이다",
    )
    v = one(make_result(t), chunk, make_source(lang="ko", country="KR"))
    assert v.relation_kind == "SPECULATIVE"
    assert v.is_correlation_only is True


def test_hedge_lexicon_is_chosen_by_source_language() -> None:
    """An English hedge word in a Korean source does not demote (HEDGE[src.lang], code section 2.5)."""
    _, chunk = en_doc_and_chunk()
    t = make_triple(
        cause=make_entity("rising power costs"),
        effect=make_entity("margins"),
        quote="rising power costs may squeeze margins",
    )
    assert one(make_result(t), chunk, make_source(lang="ko")).relation_kind == "CAUSAL"


def test_hedge_is_a_substring_match() -> None:
    """Spec section 2.5 matches substrings, so "may" inside "mayor" demotes (decision T11: kept, conservative)."""
    text = "The mayor delayed port works, which lengthens container dwell times."
    doc = make_doc(text)
    chunk = make_chunk(doc, text)
    t = make_triple(
        cause=make_entity("port works"),
        effect=make_entity("dwell times"),
        quote="The mayor delayed port works, which lengthens container dwell times",
    )
    assert one(make_result(t), chunk, make_source()).relation_kind == "SPECULATIVE"


# --- self reference ---------------------------------------------------------------------------------------------


def test_self_reference_is_dropped() -> None:
    _, chunk = en_doc_and_chunk()
    good = make_triple()
    bad = make_triple(cause=make_entity("lead times"), effect=make_entity("lead times"))
    out, drops = validate(make_result(*[good] * 7, bad), chunk, make_source())  # 1/8 dropped: under the 30% gate
    assert [v.quote for v in out] == [good.quote] * 7
    assert drops == [("self_ref", bad)]


def test_self_reference_compares_normalized_names() -> None:
    _, chunk = en_doc_and_chunk()
    bad = make_triple(cause=make_entity("Lead  Times"), effect=make_entity("Lead Times"))
    bad2 = make_triple(cause=make_entity("lead" + NBSP + "times"), effect=make_entity("lead times"))
    out, drops = validate(make_result(*[make_triple()] * 7, bad, bad2), chunk, make_source())  # 2/9 dropped
    assert len(out) == 7
    assert [reason for reason, _ in drops] == ["self_ref", "self_ref"]


def test_same_name_different_case_is_not_self_reference() -> None:
    """The spec compares `norm()` only (no casefold); a case-only difference is kept."""
    _, chunk = en_doc_and_chunk()
    t = make_triple(cause=make_entity("Lead Times"), effect=make_entity("lead times"))
    assert one(make_result(t), chunk, make_source()).cause.name == "Lead Times"


# --- grounding --------------------------------------------------------------------------------------------------


def test_ungrounded_quote_is_dropped_with_reason() -> None:
    _, chunk = en_doc_and_chunk()
    bad = make_triple(quote="tariffs on rare earths doubled battery prices overnight")
    out, drops = validate(make_result(*[make_triple()] * 7, bad), chunk, make_source())  # 1/8 dropped
    assert len(out) == 7
    assert drops == [("grounding", bad)]


def test_grounding_is_checked_before_self_reference() -> None:
    _, chunk = en_doc_and_chunk()
    bad = make_triple(
        cause=make_entity("x"),
        effect=make_entity("x"),
        quote="a sentence that is nowhere in this chunk at all",
    )
    res = make_result(*[make_triple()] * 4, bad)  # 1/5 dropped: under the gate
    _, drops = validate(res, chunk, make_source())
    assert drops == [("grounding", bad)]


def test_char_offsets_are_document_coordinates() -> None:
    doc, chunk = en_doc_and_chunk()
    assert chunk.char_start > 0
    v = one(make_result(make_triple()), chunk, make_source())
    # The model quoted single-spaced text; the document has a double space there. The span is the verbatim source span.
    assert doc.text[v.char_start : v.char_end] == "wafer shortages push  lead times higher"
    assert v.char_start == chunk.char_start + chunk.text.index("wafer shortages")
    assert chunk.char_start <= v.char_start < v.char_end <= chunk.char_end


def test_char_offsets_for_a_second_quote_in_the_chunk() -> None:
    doc, chunk = en_doc_and_chunk()
    t = make_triple(
        cause=make_entity("rising power costs"),
        effect=make_entity("margins"),
        quote="rising power costs may squeeze margins at memory makers",
    )
    v = one(make_result(t), chunk, make_source())
    assert doc.text[v.char_start : v.char_end] == "rising power costs may squeeze margins at memory makers"


def test_korean_offsets_are_document_coordinates() -> None:
    doc = make_doc(PREFIX + KO_CHUNK_TEXT, lang="ko")
    chunk = make_chunk(doc, KO_CHUNK_TEXT)
    t = make_triple(
        cause=make_entity("환율 상승"),
        effect=make_entity("수입물가"),
        quote="환율 상승이 수입물가를 끌어올렸다고 밝혔다",
    )
    v = one(make_result(t), chunk, make_source(lang="ko", country="KR"))
    assert doc.text[v.char_start : v.char_end] == "환율 상승이 수입물가를 끌어올렸다고 밝혔다"


def test_validated_triple_carries_chunk_id_and_original_fields() -> None:
    _, chunk = en_doc_and_chunk()
    t = make_triple(lag_days=14, chain_position=2, valid_at_hint="2026-11-03", targets=[make_entity("auto makers")])
    v = one(make_result(t), chunk, make_source())
    assert v.chunk_id == chunk.id
    assert v.dropped_reason is None
    for field in ("cause", "effect", "mechanism", "targets", "causal_type", "polarity", "horizon", "lag_days"):
        assert getattr(v, field) == getattr(t, field), field
    assert (v.impact, v.confidence, v.chain_position, v.valid_at_hint, v.quote) == (0.6, 0.7, 2, "2026-11-03", t.quote)


def test_validated_triple_has_no_evidence_id() -> None:
    """Mid-review decision S1: the evidence id is computed after resolve (GraphTriple), not here."""
    assert "evidence_id" not in ValidatedTriple.model_fields
    assert issubclass(ValidatedTriple, CausalTriple)


def test_validate_keeps_input_order_and_does_not_mutate_input() -> None:
    _, chunk = en_doc_and_chunk()
    a = make_triple()
    b = make_triple(
        cause=make_entity("rising power costs"),
        effect=make_entity("margins"),
        quote="rising power costs may squeeze margins",
    )
    res = make_result(a, b)
    before = res.model_dump()
    out, _ = validate(res, chunk, make_source())
    assert [v.quote for v in out] == [a.quote, b.quote]
    assert res.model_dump() == before
    assert res.triples[1].relation_kind == "CAUSAL"  # the demotion lives on the copy, not on the model output


def test_no_triples_returns_empty_without_error() -> None:
    _, chunk = en_doc_and_chunk()
    assert validate(make_result(), chunk, make_source()) == ([], [])


# --- attribution ------------------------------------------------------------------------------------------------

ATTRIBUTIVE_QUOTE = "The foundry said wafer shortages push lead times higher"


def attributive_triple(**kw: Any) -> CausalTriple:
    return make_triple(quote=ATTRIBUTIVE_QUOTE, **kw)


def test_state_controlled_attributive_fact_becomes_attribution() -> None:
    _, chunk = en_doc_and_chunk()
    v = one(make_result(attributive_triple()), chunk, make_source(state_controlled=True))
    assert v.claim_type == "attribution"


def test_non_state_source_keeps_attributive_fact() -> None:
    _, chunk = en_doc_and_chunk()
    v = one(make_result(attributive_triple()), chunk, make_source(state_controlled=False))
    assert v.claim_type == "fact"


def test_state_controlled_plain_fact_stays_fact() -> None:
    _, chunk = en_doc_and_chunk()
    v = one(make_result(make_triple()), chunk, make_source(state_controlled=True))
    assert v.claim_type == "fact"


def test_state_controlled_attribution_is_unchanged() -> None:
    _, chunk = en_doc_and_chunk()
    v = one(make_result(attributive_triple(claim_type="attribution")), chunk, make_source(state_controlled=True))
    assert v.claim_type == "attribution"


def test_korean_state_controlled_attributive_fact_becomes_attribution() -> None:
    doc = make_doc(PREFIX + KO_CHUNK_TEXT, lang="ko")
    chunk = make_chunk(doc, KO_CHUNK_TEXT)
    t = make_triple(
        cause=make_entity("환율 상승"),
        effect=make_entity("수입물가"),
        quote="환율 상승이 수입물가를 끌어올렸다고 밝혔다",
    )
    ko_state = make_source(lang="ko", country="KR", state_controlled=True)
    assert one(make_result(t), chunk, ko_state).claim_type == "attribution"
    ko_other = make_source(lang="ko", country="KR", state_controlled=False)
    assert one(make_result(t), chunk, ko_other).claim_type == "fact"


@pytest.mark.parametrize(
    ("quote", "lang", "expected"),
    [
        ("The ministry announced new export limits", "en", True),
        ("Officials SAID the move lifts prices", "en", True),
        ("According to the agency, output fell", "en", True),
        ("Wafer shortages push lead times higher", "en", False),
        ("환율 상승이 수입물가를 끌어올렸다고 밝혔다", "ko", True),
        ("정부는 새 규제를 발표했다", "ko", True),
        ("환율 상승이 수입물가를 끌어올렸다", "ko", False),
        # the lexicon is chosen by language: Korean cues do not fire for an English source and vice versa
        ("환율 상승이 수입물가를 끌어올렸다고 밝혔다", "en", False),
        ("The ministry announced new export limits", "ko", False),
    ],
)
def test_looks_attributive(quote: str, lang: str, expected: bool) -> None:
    assert looks_attributive(quote, lang) is expected


def test_unsupported_language_is_a_permanent_error() -> None:
    """W2-10 keeps non-en/ko chunks out of extraction; if one arrives anyway, fail loudly instead of skipping hedges."""
    with pytest.raises(PermanentError, match="zh"):
        looks_attributive("某句话", "zh")
    _, chunk = en_doc_and_chunk()
    with pytest.raises(PermanentError, match="zh"):
        validate(make_result(make_triple()), chunk, make_source(lang="zh", country="CN", region="CN"))


# --- drop-rate gate ---------------------------------------------------------------------------------------------


def ungrounded(i: int) -> CausalTriple:
    return make_triple(quote=f"statement number {i} that never appears in the chunk")


def test_grounding_failures_above_thirty_percent_raise() -> None:
    _, chunk = en_doc_and_chunk()
    triples = [make_triple() for _ in range(6)] + [ungrounded(i) for i in range(4)]  # 4/10
    with pytest.raises(PermanentError) as exc:
        validate(make_result(*triples), chunk, make_source())
    assert str(exc.value) == "grounding_rate_low"


def test_grounding_failures_at_thirty_percent_pass() -> None:
    _, chunk = en_doc_and_chunk()
    triples = [make_triple() for _ in range(7)] + [ungrounded(i) for i in range(3)]  # 3/10 is not > 30%
    out, drops = validate(make_result(*triples), chunk, make_source())
    assert len(out) == 7
    assert [reason for reason, _ in drops] == ["grounding"] * 3


def test_single_ungrounded_triple_raises() -> None:
    _, chunk = en_doc_and_chunk()
    with pytest.raises(PermanentError, match="grounding_rate_low"):
        validate(make_result(ungrounded(0)), chunk, make_source())


def test_self_reference_drops_count_toward_the_rate() -> None:
    """Spec 2.5 divides *all* drops by the triple count (decision T11): 2 grounding + 2 self_ref of 10 raises."""
    _, chunk = en_doc_and_chunk()
    selfref = make_triple(cause=make_entity("lead times"), effect=make_entity("lead times"))
    triples = [make_triple() for _ in range(6)] + [ungrounded(0), ungrounded(1), selfref, selfref]
    with pytest.raises(PermanentError, match="grounding_rate_low"):
        validate(make_result(*triples), chunk, make_source())
