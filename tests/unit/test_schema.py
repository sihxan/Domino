"""Unit tests for domino_extract schema, system prompt and lexicons (code-phase0.md §2.1, §2.2, §2.5; decision W2-8)."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from domino_core.errors import PermanentError
from domino_extract import lexicon
from domino_extract.lexicon import ATTRIBUTION, HEDGE, load_lexicon
from domino_extract.prompts import EXTRACT_SYSTEM
from domino_extract.schema import PROMPT_VERSION, CausalTriple, EntityRef, EventHint, ExtractionResult

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "docs" / "spec" / "code-phase0.md"
PROMPT_FILE = ROOT / "packages" / "domino_extract" / "prompts" / "extract_system.md"
PKG = ROOT / "packages" / "domino_extract"

# CLAUDE.md rule 10: investment-advice wording must not appear in prompts, templates or test data.
FORBIDDEN_ADVICE = ("매수", "매도", "추천", "목표가", "확실히")


def spec_prompt_block() -> str:
    """The ```markdown block under '### 2.2' of code-phase0.md, without the fences."""
    spec = SPEC.read_text(encoding="utf-8")
    start_heading = spec.index("### 2.2 ")
    open_fence = spec.index("```markdown\n", start_heading) + len("```markdown\n")
    close_fence = spec.index("\n```\n", open_fence)
    return spec[open_fence:close_fence]


def spec_example_outputs() -> list[Any]:
    """The JSON after each 'Output:' in the §2.2 examples, parsed."""
    block = spec_prompt_block()
    examples_at = block.index("## Examples")
    parts = re.split(r"^### Example \d+ .*$", block[examples_at:], flags=re.MULTILINE)[1:]
    outputs = []
    for part in parts:
        raw = part.split("Output:", 1)[1].strip()
        outputs.append(json.loads(raw))
    return outputs


def make_entity(name: str = "A", **kw: Any) -> dict[str, Any]:
    return {"name": name, "type": "INDICATOR", "direction": "UP", "region": "US"} | kw


def make_triple(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "cause": make_entity("A"),
        "effect": make_entity("B"),
        "mechanism": "A raises B",
        "targets": [],
        "relation_kind": "CAUSAL",
        "causal_type": "DIRECT",
        "polarity": "POSITIVE",
        "horizon": "short",
        "lag_days": None,
        "impact": 0.5,
        "confidence": 0.5,
        "claim_type": "fact",
        "chain_position": None,
        "valid_at_hint": None,
        "quote": "A raises B",
    }
    return base | kw


def make_result(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"event": None, "triples": [make_triple()], "no_causal_content": False}
    return base | kw


# --- spec examples ----------------------------------------------------------------------------------------------


def test_spec_has_three_examples() -> None:
    assert len(spec_example_outputs()) == 3


@pytest.mark.parametrize("n", [1, 2, 3])
def test_spec_example_output_validates(n: int) -> None:
    out = spec_example_outputs()[n - 1]
    res = ExtractionResult.model_validate(out)
    assert res.no_causal_content is (n == 3)


def test_spec_example_contents() -> None:
    ex1, ex2, ex3 = (ExtractionResult.model_validate(o) for o in spec_example_outputs())
    assert [t.chain_position for t in ex1.triples] == [1, 2, 3]
    assert ex1.triples[2].targets[0].name == "Siemens Energy"
    assert ex1.event is None
    assert [t.relation_kind for t in ex2.triples] == ["CORRELATION", "SPECULATIVE"]
    assert ex2.event is not None and ex2.event.category == "MACRO"
    assert ex3.triples == [] and ex3.event is not None and ex3.event.action_type == "MAKE_STATEMENT"


def test_prompt_file_examples_match_spec_examples() -> None:
    """The prompt the model sees carries the same example JSON the schema validates (no drift)."""
    block = EXTRACT_SYSTEM
    examples_at = block.index("## Examples")
    parts = re.split(r"^### Example \d+ .*$", block[examples_at:], flags=re.MULTILINE)[1:]
    outs = [json.loads(p.split("Output:", 1)[1].strip()) for p in parts]
    assert outs == spec_example_outputs()


# --- constraints ------------------------------------------------------------------------------------------------


def test_no_causal_content_with_triples_rejected() -> None:
    with pytest.raises(ValidationError, match="no_causal_content with triples"):
        ExtractionResult.model_validate(make_result(no_causal_content=True))


def test_no_causal_content_without_triples_accepted() -> None:
    res = ExtractionResult.model_validate(make_result(no_causal_content=True, triples=[]))
    assert res.no_causal_content and res.triples == []


def test_empty_triples_without_flag_accepted() -> None:
    assert ExtractionResult.model_validate(make_result(triples=[])).triples == []


def test_ten_triples_accepted_eleven_rejected() -> None:
    assert len(ExtractionResult.model_validate(make_result(triples=[make_triple()] * 10)).triples) == 10
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(make_result(triples=[make_triple()] * 11))


@pytest.mark.parametrize("field", ["impact", "confidence"])
@pytest.mark.parametrize(("value", "ok"), [(0.0, True), (1.0, True), (1.1, False), (-0.1, False)])
def test_unit_interval_fields(field: str, value: float, ok: bool) -> None:
    data = make_triple(**{field: value})
    if ok:
        assert getattr(CausalTriple.model_validate(data), field) == value
    else:
        with pytest.raises(ValidationError):
            CausalTriple.model_validate(data)


@pytest.mark.parametrize(
    ("field", "limit"),
    [("mechanism", 200), ("quote", 300)],
)
def test_triple_string_length_limits(field: str, limit: int) -> None:
    CausalTriple.model_validate(make_triple(**{field: "x" * limit}))
    with pytest.raises(ValidationError):
        CausalTriple.model_validate(make_triple(**{field: "x" * (limit + 1)}))


def test_entity_name_limits() -> None:
    EntityRef.model_validate(make_entity("x" * 120))
    with pytest.raises(ValidationError):
        EntityRef.model_validate(make_entity("x" * 121))
    with pytest.raises(ValidationError):
        EntityRef.model_validate(make_entity(""))


def test_entity_region_defaults_to_global() -> None:
    data = make_entity()
    del data["region"]
    assert EntityRef.model_validate(data).region == "GL"


def test_targets_at_most_three() -> None:
    CausalTriple.model_validate(make_triple(targets=[make_entity("t")] * 3))
    with pytest.raises(ValidationError):
        CausalTriple.model_validate(make_triple(targets=[make_entity("t")] * 4))


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("relation_kind", "CAUSES"),
        ("causal_type", "DIRECTLY"),
        ("polarity", "NONE"),
        ("horizon", "medium"),
        ("claim_type", "opinion"),
    ],
)
def test_enum_fields_reject_unknown_values(field: str, bad: str) -> None:
    with pytest.raises(ValidationError):
        CausalTriple.model_validate(make_triple(**{field: bad}))


@pytest.mark.parametrize("field", ["type", "direction"])
def test_entity_enums_reject_unknown_values(field: str) -> None:
    with pytest.raises(ValidationError):
        EntityRef.model_validate(make_entity(**{field: "BOGUS"}))


def test_triple_optional_fields_default() -> None:
    data = make_triple()
    for k in ("targets", "lag_days", "chain_position", "valid_at_hint"):
        del data[k]
    t = CausalTriple.model_validate(data)
    assert t.targets == [] and t.lag_days is None and t.chain_position is None and t.valid_at_hint is None


def test_event_hint_limits() -> None:
    ok = {"title": "x" * 120, "category": "MACRO", "actors": ["a"] * 4}
    hint = EventHint.model_validate(ok)
    assert hint.country == "GL" and hint.occurred_at is None and hint.scheduled_at is None
    with pytest.raises(ValidationError):
        EventHint.model_validate(ok | {"title": "x" * 121})
    with pytest.raises(ValidationError):
        EventHint.model_validate(ok | {"actors": ["a"] * 5})
    with pytest.raises(ValidationError):
        EventHint.model_validate(ok | {"category": "SPORTS"})


def test_event_key_is_required() -> None:
    data = make_result()
    del data["event"]
    with pytest.raises(ValidationError):
        ExtractionResult.model_validate(data)


def test_roundtrip_json() -> None:
    res = ExtractionResult.model_validate(make_result())
    assert ExtractionResult.model_validate_json(res.model_dump_json()) == res
    assert copy.deepcopy(res.model_dump()) == res.model_dump()


# --- version and prompt -----------------------------------------------------------------------------------------


def test_prompt_version() -> None:
    assert PROMPT_VERSION == "ext-1.0"


def test_extract_system_md_matches_spec_block_exactly() -> None:
    """extract_system.md is the §2.2 code block, character for character (plus a text file's one trailing newline)."""
    assert PROMPT_FILE.read_text(encoding="utf-8") == spec_prompt_block() + "\n"


def test_extract_system_constant_is_the_file() -> None:
    assert PROMPT_FILE.read_text(encoding="utf-8") == EXTRACT_SYSTEM
    assert EXTRACT_SYSTEM.startswith("You are a causal-relation extraction engine")


def test_prompt_hard_rules_are_numbered_one_to_twelve() -> None:
    rules_section = EXTRACT_SYSTEM.split("## Hard rules")[1].split("## Examples")[0]
    rules = re.findall(r"^(\d+)\. ", rules_section, flags=re.MULTILINE)
    assert rules == [str(i) for i in range(1, 13)]


def test_no_investment_advice_wording() -> None:
    texts = {
        "prompt": EXTRACT_SYSTEM,
        "hedge": (PKG / "hedge_lexicon.yaml").read_text(encoding="utf-8"),
        "attribution": (PKG / "attribution_lexicon.yaml").read_text(encoding="utf-8"),
    }
    for name, text in texts.items():
        for word in FORBIDDEN_ADVICE:
            assert word not in text, f"{word!r} in {name}"


# --- lexicons ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lex", [HEDGE, ATTRIBUTION], ids=["hedge", "attribution"])
def test_lexicons_have_en_and_ko(lex: dict[str, list[str]]) -> None:
    assert {"en", "ko"} <= set(lex)
    for lang in ("en", "ko"):
        assert lex[lang] and all(isinstance(w, str) and w.strip() == w and w for w in lex[lang])


@pytest.mark.parametrize("lex", [HEDGE, ATTRIBUTION], ids=["hedge", "attribution"])
def test_lexicon_entries_are_lowercase(lex: dict[str, list[str]]) -> None:
    """Callers match against `quote.lower()`, so entries must already be lowercase."""
    for words in lex.values():
        assert all(w == w.lower() for w in words)


def test_hedge_lexicon_starts_from_spec_list() -> None:
    assert HEDGE["en"] == ["could", "may", "might", "expected to", "likely", "forecast", "potential"]
    assert HEDGE["ko"] == ["전망", "가능성", "우려", "예상", "관측", "듯"]


def test_attribution_lexicon_is_w2_8_list() -> None:
    assert ATTRIBUTION["en"] == [
        "said",
        "says",
        "announced",
        "stated",
        "according to",
        "claimed",
        "told",
        "noted",
        "pledged",
    ]
    assert ATTRIBUTION["ko"] == ["밝혔다", "발표", "말했다", "따르면", "강조했다", "주장", "언급"]


def test_load_lexicon_reads_the_packaged_yaml() -> None:
    assert load_lexicon("hedge_lexicon.yaml") == HEDGE
    assert load_lexicon("attribution_lexicon.yaml") == ATTRIBUTION


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "lex.yaml"
    p.write_text(body, encoding="utf-8")
    return p


@pytest.mark.parametrize(
    "body",
    [
        "- just\n- a list\n",  # not a mapping
        "en: [could]\n",  # ko missing
        "ko: [전망]\n",  # en missing
        "en: could\nko: [전망]\n",  # value not a list
        "en: [could, '']\nko: [전망]\n",  # empty entry
        "en: [could, 3]\nko: [전망]\n",  # non-string entry
        "en: [Could]\nko: [전망]\n",  # uppercase entry
        "en: []\nko: [전망]\n",  # empty language list
        "en: [could\n",  # broken YAML
    ],
)
def test_load_lexicon_rejects_malformed(tmp_path: Path, body: str) -> None:
    with pytest.raises(PermanentError):
        load_lexicon(_write(tmp_path, body))


def test_load_lexicon_accepts_extra_languages(tmp_path: Path) -> None:
    lex = load_lexicon(_write(tmp_path, "en: [could]\nko: [전망]\nde: [könnte]\n"))
    assert lex["de"] == ["könnte"]


def test_lexicon_yaml_files_are_plain_mappings() -> None:
    for name in ("hedge_lexicon.yaml", "attribution_lexicon.yaml"):
        data = yaml.safe_load((PKG / name).read_text(encoding="utf-8"))
        assert isinstance(data, dict) and set(data) >= {"en", "ko"}


def test_module_exports() -> None:
    assert lexicon.HEDGE is HEDGE and lexicon.ATTRIBUTION is ATTRIBUTION
