"""Unit tests for domino_eval: gold loader, Metrics on a hand-computed fixture, thresholds, regression run, export.

Everything here runs without a real LLM: `llm.call` is replaced by a scripted fake.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from domino_core.clients import llm
from domino_core.errors import PermanentError
from domino_core.timeutil import report_date
from domino_eval.gold import GoldItem, load_gold
from domino_eval.gold_export import ExportRow, blank_item, select_chunks, write_items
from domino_eval.regression import (
    Metrics,
    Prediction,
    assert_thresholds,
    compute_metrics,
    failed_thresholds,
    run_extract_regression,
)
from domino_extract.schema import PROMPT_VERSION, CausalTriple, ExtractionResult

BASE_ENV = {
    "PG_DSN": "postgresql+psycopg://u:p@localhost:5432/domino",
    "NEO4J_URI": "bolt://localhost:7687",
    "NEO4J_USER": "neo4j",
    "NEO4J_PASSWORD": "x",
    "REDIS_URL": "redis://localhost:6379/0",
    "S3_ENDPOINT": "http://localhost:9000",
    "S3_KEY": "k",
    "S3_SECRET": "s",
    "LANGFUSE_HOST": "http://localhost:3000",
    "LANGFUSE_PUBLIC_KEY": "pk",
    "LANGFUSE_SECRET_KEY": "sk",
    "RESEND_API_KEY": "r",
    "FROM_EMAIL": "a@example.com",
    "MODEL_EXTRACT": "model-for-test",
}

# --- builders -----------------------------------------------------------------------------------


def pt(
    quote: str,
    cause: str,
    effect: str,
    *,
    dir: str = "UP",
    rk: str = "CAUSAL",
    ct: str = "DIRECT",
    conf: float = 0.5,
) -> dict[str, Any]:
    """An extractor triple as the model would emit it (dict form)."""
    return {
        "cause": {"name": cause, "type": "INFRA", "direction": "DOWN", "region": "US"},
        "effect": {"name": effect, "type": "INDICATOR", "direction": dir, "region": "US"},
        "mechanism": "test mechanism",
        "targets": [],
        "relation_kind": rk,
        "causal_type": ct,
        "polarity": "POSITIVE",
        "horizon": "short",
        "lag_days": None,
        "impact": 0.5,
        "confidence": conf,
        "claim_type": "fact",
        "chain_position": None,
        "valid_at_hint": None,
        "quote": quote,
    }


def gt(
    quote: str,
    cause: str,
    effect: str,
    *,
    dir: str = "UP",
    rk: str = "CAUSAL",
    ct: str = "DIRECT",
) -> dict[str, Any]:
    """A gold triple in the jsonl label form (code section 7.1)."""
    return {
        "cause": cause,
        "effect": effect,
        "direction_effect": dir,
        "relation_kind": rk,
        "causal_type": ct,
        "horizon": "short",
        "claim_type": "fact",
        "quote": quote,
    }


def gold_dict(
    chunk_id: str, lang: str, text: str, has_causal: bool | None, triples: list[dict[str, Any]], **kw: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "chunk_id": chunk_id,
        "lang": lang,
        "text": text,
        "source": {"publisher": "Test Publisher", "grade": "A", "state_controlled": False},
        "labels": {"has_causal": has_causal, "triples": triples},
        "annotators": ["a"],
        "kappa": None,
    }
    return base | kw


def write_gold(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
    return path


def preds_of(*triples: dict[str, Any], emitted: int | None = None, ungrounded: int = 0) -> Prediction:
    ts = [CausalTriple.model_validate(t) for t in triples]
    n_emitted = len(ts) + ungrounded if emitted is None else emitted
    return Prediction(triples=ts, n_emitted=n_emitted, n_ungrounded=ungrounded)


# --- Metrics on a hand-computed 3-chunk fixture -------------------------------------------------

Q1 = "wafer shortages push lead times higher"
Q2 = "rising power costs squeeze margins"
Q3 = "금리가 오르면서 환율도 동반 상승했다"
Q4 = "tariffs raise import prices"
Q5 = "demand for chips keeps growing"


def fixture_items() -> list[GoldItem]:
    return [
        GoldItem.model_validate(
            gold_dict(
                "gold_0001",
                "en",
                f"{Q1}. {Q2}. {Q4}.",
                True,
                [
                    gt(Q1, "wafer shortages", "lead times", dir="UP", rk="CAUSAL", ct="BOTTLENECK"),
                    gt(Q2, "power costs", "memory margins", dir="DOWN", rk="SPECULATIVE", ct="DIRECT"),
                ],
            )
        ),
        GoldItem.model_validate(
            gold_dict("gold_0002", "ko", Q3, True, [gt(Q3, "금리", "환율", dir="UP", rk="CORRELATION", ct="DIRECT")])
        ),
        GoldItem.model_validate(gold_dict("gold_0003", "en", Q5, False, [])),
    ]


def fixture_predictions() -> list[Prediction]:
    return [
        # chunk 1: two pairs (second one wrong rk + causal_type), one FP; the model also emitted 1 ungrounded quote
        preds_of(
            pt(Q1, "wafer shortages", "lead times", dir="UP", rk="CAUSAL", ct="BOTTLENECK", conf=0.9),
            pt(Q2, "power costs", "memory margins", dir="DOWN", rk="CAUSAL", ct="ENABLING", conf=0.5),
            pt(Q4, "tariffs", "import prices", dir="UP", rk="CAUSAL", ct="DIRECT", conf=0.85),
            ungrounded=1,
        ),
        # chunk 2: one pair with the wrong effect direction
        preds_of(pt(Q3, "금리", "환율", dir="DOWN", rk="CORRELATION", ct="DIRECT", conf=0.7)),
        # chunk 3: gold says no causal content, the model found a triple (Task-1 false positive)
        preds_of(pt(Q5, "chip demand", "capacity", dir="UP", rk="CAUSAL", ct="DIRECT", conf=0.8)),
    ]


def test_metrics_on_the_hand_computed_fixture() -> None:
    m = compute_metrics(fixture_items(), fixture_predictions())
    # emitted 4+1+1 = 6 quotes, 1 not found -> 5/6
    assert m.grounding_rate == pytest.approx(5 / 6)
    # pairs (gold -> pred): CAUSAL->CAUSAL, SPECULATIVE->CAUSAL, CORRELATION->CORRELATION.
    # CAUSAL P=1/2 R=1 F1=2/3; SPECULATIVE F1=0; CORRELATION F1=1; CONDITIONAL has no support -> left out: (2/3+0+1)/3
    assert m.rk_macro_f1 == pytest.approx(5 / 9)
    assert m.dir_acc == pytest.approx(2 / 3)  # UP/UP, DOWN/DOWN, DOWN/UP
    assert m.causal_type_acc == pytest.approx(2 / 3)
    # Task-1: chunks 1, 2 true positive, chunk 3 false positive -> P=2/3 R=1 F1=0.8
    assert m.task1_f1 == pytest.approx(0.8)
    assert m.triples_per_chunk == pytest.approx(5 / 3)  # 3 + 1 + 1 validated triples over 3 chunks
    assert m.gold_triples_per_chunk == pytest.approx(1.0)  # 2 + 1 + 0 over 3 chunks
    assert m.conf_over_08_ratio == pytest.approx(3 / 5)  # 0.9, 0.85, 0.8 of 0.9, 0.5, 0.85, 0.7, 0.8 (>= 0.8)
    assert (m.n_chunks, m.n_gold_triples, m.n_pred_triples, m.n_matched) == (3, 3, 5, 3)


def test_metrics_with_no_predictions_do_not_divide_by_zero() -> None:
    m = compute_metrics(fixture_items(), [Prediction([], 0, 0)] * 3)
    assert m.grounding_rate == 0.0  # nothing was extracted: not measurable is not a pass
    assert m.rk_macro_f1 == 0.0 and m.dir_acc == 0.0 and m.causal_type_acc == 0.0
    assert m.task1_f1 == 0.0
    assert m.triples_per_chunk == 0.0 and m.conf_over_08_ratio == 0.0
    assert m.n_matched == 0


def test_perfect_predictions_score_one() -> None:
    items = fixture_items()
    perfect = [
        preds_of(
            pt(Q1, "wafer shortages", "lead times", dir="UP", rk="CAUSAL", ct="BOTTLENECK"),
            pt(Q2, "power costs", "memory margins", dir="DOWN", rk="SPECULATIVE", ct="DIRECT"),
        ),
        preds_of(pt(Q3, "금리", "환율", dir="UP", rk="CORRELATION", ct="DIRECT")),
        preds_of(),
    ]
    m = compute_metrics(items, perfect)
    assert (m.grounding_rate, m.rk_macro_f1, m.dir_acc, m.task1_f1) == (1.0, 1.0, 1.0, 1.0)
    assert m.triples_per_chunk == m.gold_triples_per_chunk == 1.0


def test_metrics_need_one_prediction_per_gold_item() -> None:
    with pytest.raises(PermanentError):
        compute_metrics(fixture_items(), fixture_predictions()[:2])


# --- thresholds (tech section 7.2) --------------------------------------------------------------

AT_LIMIT: dict[str, Any] = {
    "grounding_rate": 0.95,
    "rk_macro_f1": 0.80,
    "dir_acc": 0.85,
    "task1_f1": 0.85,
    "triples_per_chunk": 1.3,
    "gold_triples_per_chunk": 1.0,
    "conf_over_08_ratio": 0.60,
}


def metrics(**override: float) -> Metrics:
    return Metrics(**(AT_LIMIT | override))


def test_every_threshold_passes_exactly_at_its_limit() -> None:
    assert_thresholds(metrics())
    assert failed_thresholds(metrics()) == []


@pytest.mark.parametrize(
    ("name", "just_failing"),
    [
        ("grounding_rate", 0.9499),
        ("rk_macro_f1", 0.7999),
        ("dir_acc", 0.8499),
        ("task1_f1", 0.8499),
        ("conf_over_08_ratio", 0.6001),
        ("triples_per_chunk", 1.31),  # more than +30% of gold
    ],
)
def test_each_threshold_fails_just_past_its_limit(name: str, just_failing: float) -> None:
    m = metrics(**{name: just_failing})
    assert [f.split()[0] for f in failed_thresholds(m)] == [name]
    with pytest.raises(AssertionError, match=name):
        assert_thresholds(m)


def test_triples_per_chunk_band_is_gold_plus_minus_30_percent() -> None:
    assert_thresholds(metrics(triples_per_chunk=0.7))  # -30% passes
    for bad in (0.69, 1.31):
        with pytest.raises(AssertionError, match="triples_per_chunk"):
            assert_thresholds(metrics(triples_per_chunk=bad))


def test_triples_per_chunk_with_zero_gold_triples_must_be_zero() -> None:
    assert_thresholds(metrics(triples_per_chunk=0.0, gold_triples_per_chunk=0.0))
    with pytest.raises(AssertionError, match="triples_per_chunk"):
        assert_thresholds(metrics(triples_per_chunk=0.1, gold_triples_per_chunk=0.0))


def test_assert_thresholds_reports_every_failing_metric() -> None:
    m = metrics(grounding_rate=0.5, dir_acc=0.5)
    with pytest.raises(AssertionError) as exc:
        assert_thresholds(m)
    assert "grounding_rate" in str(exc.value) and "dir_acc" in str(exc.value)


# --- gold loader --------------------------------------------------------------------------------


def good_rows() -> list[dict[str, Any]]:
    return [
        gold_dict("gold_0001", "en", Q1, True, [gt(Q1, "wafer shortages", "lead times")]),
        gold_dict("gold_0002", "ko", "삼성전자는 매출을 공시했다.", False, []),
    ]


def test_load_gold_reads_items(tmp_path: Path) -> None:
    items = load_gold(write_gold(tmp_path / "g.jsonl", good_rows()))
    assert [i.chunk_id for i in items] == ["gold_0001", "gold_0002"]
    assert items[0].labels.triples[0].direction_effect == "UP"
    assert items[1].labels.has_causal is False and items[1].lang == "ko"
    assert items[0].annotators == ["a"] and items[0].kappa is None


def test_load_gold_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "g.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in good_rows()) + "\n\n\n", encoding="utf-8")
    assert len(load_gold(path)) == 2


def test_load_gold_missing_file_is_a_clear_error(tmp_path: Path) -> None:
    with pytest.raises(PermanentError, match="not found"):
        load_gold(tmp_path / "nope.jsonl")


def test_load_gold_names_the_bad_line(tmp_path: Path) -> None:
    path = tmp_path / "g.jsonl"
    path.write_text(json.dumps(good_rows()[0]) + "\n{not json\n", encoding="utf-8")
    with pytest.raises(PermanentError, match="line 2"):
        load_gold(path)


def test_load_gold_rejects_unlabeled_items(tmp_path: Path) -> None:
    rows = good_rows()
    rows[1]["labels"]["has_causal"] = None
    with pytest.raises(PermanentError, match="unlabeled"):
        load_gold(write_gold(tmp_path / "g.jsonl", rows))


def test_load_gold_rejects_duplicate_chunk_ids(tmp_path: Path) -> None:
    rows = good_rows()
    rows[1]["chunk_id"] = "gold_0001"
    with pytest.raises(PermanentError, match="duplicate"):
        load_gold(write_gold(tmp_path / "g.jsonl", rows))


def test_load_gold_rejects_inconsistent_labels(tmp_path: Path) -> None:
    no_triples = good_rows()
    no_triples[0]["labels"]["triples"] = []  # has_causal true, nothing labeled
    with pytest.raises(PermanentError, match="line 1"):
        load_gold(write_gold(tmp_path / "a.jsonl", no_triples))
    with_triples = good_rows()
    with_triples[1]["labels"]["triples"] = [gt("q", "a", "b")]  # has_causal false but a triple
    with pytest.raises(PermanentError, match="line 2"):
        load_gold(write_gold(tmp_path / "b.jsonl", with_triples))


def test_load_gold_rejects_unknown_fields_and_values(tmp_path: Path) -> None:
    typo = good_rows()
    typo[0]["labels"]["triples"][0]["directon_effect"] = "UP"  # a typo must not silently drop a label
    with pytest.raises(PermanentError):
        load_gold(write_gold(tmp_path / "a.jsonl", typo))
    bad_kind = good_rows()
    bad_kind[0]["labels"]["triples"][0]["relation_kind"] = "MAYBE"
    with pytest.raises(PermanentError):
        load_gold(write_gold(tmp_path / "b.jsonl", bad_kind))
    zh = good_rows()
    zh[0]["lang"] = "zh"
    with pytest.raises(PermanentError):
        load_gold(write_gold(tmp_path / "c.jsonl", zh))


def test_load_gold_requires_an_annotator(tmp_path: Path) -> None:
    rows = good_rows()
    rows[0]["annotators"] = []
    with pytest.raises(PermanentError):
        load_gold(write_gold(tmp_path / "g.jsonl", rows))


# --- run_extract_regression with a scripted llm.call --------------------------------------------

HINT_TEXT = f"{Q1} for automotive chips, and {Q2}."  # has the hint words "shortage" and "push"
NO_HINT_TEXT = "The committee met on Tuesday and published its calendar."


@dataclass
class ScriptedLLM:
    """Routes `classify` to a fixed answer and `extract` to per-chunk scripted outcomes (keyed by user text)."""

    extract: dict[str, list[llm.LLMResult[ExtractionResult] | Exception]] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kw: Any) -> Any:
        self.calls.append(kw)
        if kw["task"] == "classify":
            return llm.LLMResult(parsed=None, raw="Y", input_tokens=10, output_tokens=1, cost_usd=0.0001, cache_read=0)
        for needle, outcomes in self.extract.items():
            if needle in kw["user"]:
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    raise outcome
                return outcome
        raise AssertionError(f"no scripted outcome for {kw['user'][:80]!r}")


def reply(*triples: dict[str, Any], cost: float = 0.001) -> llm.LLMResult[ExtractionResult]:
    payload = {"event": None, "triples": list(triples), "no_causal_content": not triples}
    return llm.LLMResult(
        parsed=ExtractionResult.model_validate(payload),
        raw=json.dumps(payload),
        input_tokens=1000,
        output_tokens=200,
        cost_usd=cost,
        cache_read=800,
    )


@pytest.fixture
def scripted(monkeypatch: pytest.MonkeyPatch) -> ScriptedLLM:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    f = ScriptedLLM()
    monkeypatch.setattr(llm, "call", f)
    return f


def regression_gold(tmp_path: Path) -> Path:
    return write_gold(
        tmp_path / "g.jsonl",
        [
            gold_dict("gold_0001", "en", HINT_TEXT, True, [gt(Q1, "wafer shortages", "lead times")]),
            gold_dict("gold_0002", "en", NO_HINT_TEXT, False, []),
            gold_dict("gold_0003", "en", HINT_TEXT + " Again.", True, [gt(Q2, "power costs", "margins")]),
        ],
    )


def test_run_extract_regression_end_to_end(tmp_path: Path, scripted: ScriptedLLM) -> None:
    scripted.extract = {
        # chunk 1: two grounded triples, one of them is the gold triple
        f"{HINT_TEXT}\n</document>": [
            reply(pt(Q1, "wafer shortages", "lead times", conf=0.8), pt(Q2, "power costs", "margins", conf=0.8))
        ],
        # chunk 3: both attempts fail the schema -> ExtractFailed -> no triples for that chunk
        f"{HINT_TEXT} Again.\n</document>": [PermanentError("schema: bad"), PermanentError("schema: bad")],
    }
    m = run_extract_regression(regression_gold(tmp_path), "model-for-test", PROMPT_VERSION)
    assert m.n_chunks == 3 and m.n_gold_triples == 2 and m.n_pred_triples == 2 and m.n_matched == 1
    assert m.grounding_rate == 1.0
    assert m.rk_macro_f1 == 1.0 and m.dir_acc == 1.0
    assert m.task1_f1 == pytest.approx(2 / 3)  # chunk 1 hit, chunk 2 correct, chunk 3 (failed) missed: P=1 R=1/2
    assert m.triples_per_chunk == pytest.approx(2 / 3) and m.gold_triples_per_chunk == pytest.approx(2 / 3)
    assert m.conf_over_08_ratio == 1.0
    assert m.extract_cost_usd == pytest.approx(0.001)  # the failed chunk reported no usage
    tasks = [c["task"] for c in scripted.calls]
    assert tasks.count("classify") == 2  # chunk 2 has no hint word: no LLM call at all
    assert tasks.count("extract") == 3  # 1 + 2 failed attempts


def test_run_extract_regression_spends_the_golden_budget(tmp_path: Path, scripted: ScriptedLLM) -> None:
    scripted.extract = {f"{HINT_TEXT}\n</document>": [reply(pt(Q1, "wafer shortages", "lead times"))]}
    gold_path = write_gold(
        tmp_path / "g.jsonl", [gold_dict("gold_0001", "en", HINT_TEXT, True, [gt(Q1, "wafer shortages", "lead times")])]
    )
    original = llm.call
    run_extract_regression(gold_path, "model-for-test", PROMPT_VERSION)
    assert llm.call is original  # the temporary budget-key wrapper is removed again
    keys = {c["budget_key"] for c in scripted.calls}
    assert keys == {f"budget:golden:{report_date().isoformat()}"}  # not the production extract/classify budgets


def test_run_extract_regression_counts_ungrounded_quotes_of_a_failed_chunk(
    tmp_path: Path, scripted: ScriptedLLM
) -> None:
    ghost = pt("a quote that is nowhere in the chunk", "wafer shortages", "lead times")
    scripted.extract = {
        f"{HINT_TEXT}\n</document>": [reply(pt(Q1, "wafer shortages", "lead times"), ghost, ghost)] * 2
    }  # 2/3 ungrounded on both attempts -> ExtractFailed(grounding)
    gold_path = write_gold(
        tmp_path / "g.jsonl", [gold_dict("gold_0001", "en", HINT_TEXT, True, [gt(Q1, "wafer shortages", "lead times")])]
    )
    m = run_extract_regression(gold_path, "model-for-test", PROMPT_VERSION)
    assert m.n_pred_triples == 0
    assert m.grounding_rate == pytest.approx(1 / 3)  # the failed chunk's quotes still count against grounding


def test_run_extract_regression_refuses_a_model_or_prompt_it_would_not_use(
    tmp_path: Path, scripted: ScriptedLLM
) -> None:
    path = regression_gold(tmp_path)
    with pytest.raises(PermanentError, match="model"):
        run_extract_regression(path, "some-other-model", PROMPT_VERSION)
    with pytest.raises(PermanentError, match="prompt_version"):
        run_extract_regression(path, "model-for-test", "ext-9.9")
    assert scripted.calls == []


def test_run_extract_regression_missing_gold_fails_before_any_call(tmp_path: Path, scripted: ScriptedLLM) -> None:
    with pytest.raises(PermanentError, match="not found"):
        run_extract_regression(tmp_path / "missing.jsonl", "model-for-test", PROMPT_VERSION)
    assert scripted.calls == []


# --- gold_export --------------------------------------------------------------------------------


def row(chunk_id: str, doc: str, src: str, lang: str = "ko") -> ExportRow:
    return ExportRow(
        chunk_id=chunk_id, doc_id=doc, source_id=src, lang=lang, text=f"text of {chunk_id}",
        publisher=f"pub {src}", grade="A", state_controlled=False,
    )  # fmt: skip


def sample_rows() -> list[ExportRow]:
    return [
        row("chk_a1x", "doc_a1", "src_a"), row("chk_a1y", "doc_a1", "src_a"), row("chk_a2", "doc_a2", "src_a"),
        row("chk_b1", "doc_b1", "src_b"), row("chk_c1", "doc_c1", "src_c"),
    ]  # fmt: skip


def test_select_chunks_spreads_over_sources_and_documents() -> None:
    picked = select_chunks(sample_rows(), 4)
    assert len(picked) == 4
    assert {r.source_id for r in picked} == {"src_a", "src_b", "src_c"}
    assert len({r.doc_id for r in picked}) == 4  # one chunk per document first (overlapping chunks are correlated)


def test_select_chunks_falls_back_to_second_chunks_of_a_document() -> None:
    assert len(select_chunks(sample_rows(), 5)) == 5
    assert {r.chunk_id for r in select_chunks(sample_rows(), 5)} == {r.chunk_id for r in sample_rows()}


def test_select_chunks_returns_everything_when_n_exceeds_the_pool() -> None:
    assert len(select_chunks(sample_rows(), 99)) == 5


def test_select_chunks_is_deterministic_and_independent_of_input_order() -> None:
    a = [r.chunk_id for r in select_chunks(sample_rows(), 3)]
    b = [r.chunk_id for r in select_chunks(list(reversed(sample_rows())), 3)]
    assert a == b


def test_blank_item_has_empty_labels_and_matches_the_gold_layout() -> None:
    item = blank_item(row("chk_a1x", "doc_a1", "src_a"), "gold_0001")
    assert item == {
        "chunk_id": "gold_0001",
        "lang": "ko",
        "text": "text of chk_a1x",
        "source": {"publisher": "pub src_a", "grade": "A", "state_controlled": False},
        "labels": {"has_causal": None, "triples": []},
        "annotators": [],
        "kappa": None,
    }


def test_write_items_never_overwrites_a_file(tmp_path: Path) -> None:
    out = tmp_path / "labels.jsonl"
    out.write_text("human work\n", encoding="utf-8")
    with pytest.raises(PermanentError, match="exists"):
        write_items(out, [blank_item(row("chk_a1x", "doc_a1", "src_a"), "gold_0001")])
    assert out.read_text(encoding="utf-8") == "human work\n"


def test_exported_file_is_utf8_jsonl_that_load_gold_calls_unlabeled(tmp_path: Path) -> None:
    out = tmp_path / "sub" / "ko.jsonl"
    items = [blank_item(r, f"gold_{i:04d}") for i, r in enumerate(select_chunks(sample_rows(), 2), start=1)]
    write_items(out, items)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["labels"]["has_causal"] is None
    with pytest.raises(PermanentError, match="unlabeled"):
        load_gold(out)
