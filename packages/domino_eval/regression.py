"""Extraction regression on the gold set: run the pipeline, match to labels, compute Metrics, check tech section 7.2."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, get_args

from pydantic import ValidationError

from domino_core.clients import default_settings, llm
from domino_core.errors import PermanentError
from domino_core.log import get_logger
from domino_core.models import Chunk, SourceSpec
from domino_core.timeutil import report_date
from domino_eval.gold import GoldItem, load_gold
from domino_eval.matching import match_triples
from domino_extract.classifier import classify
from domino_extract.extractor import ExtractFailed, extract
from domino_extract.grounding import ground
from domino_extract.schema import PROMPT_VERSION, CausalTriple, ExtractionResult, RelKind

log = get_logger(__name__)

# tech section 7.2 thresholds
GROUNDING_MIN = 0.95
RK_MACRO_F1_MIN = 0.80
DIR_ACC_MIN = 0.85
TASK1_F1_MIN = 0.85
TRIPLES_REL_TOL = 0.30  # chunk-average triple count within gold +/- 30%
CONF_OVER_08_MAX = 0.60
CONF_HIGH = 0.8  # "0.8 or more" counts as high confidence
_EPS = 1e-9  # 1.3 - 1.0 is 0.30000000000000004 in floats; the band is inclusive

REL_KINDS: tuple[str, ...] = get_args(RelKind)


@dataclass(frozen=True)
class Prediction:
    """What the pipeline produced for one gold chunk.

    `triples` are the validated ones; `n_emitted` is how many the model returned and `n_ungrounded` how many of those
    had a quote that is not in the chunk. A chunk skipped by the classifier or failed in extraction has no triples.
    """

    triples: Sequence[CausalTriple]
    n_emitted: int
    n_ungrounded: int
    cost_usd: float = 0.0


@dataclass(frozen=True)
class Metrics:
    """The six metrics of code section 7.2, the gold triple rate they are compared with, and run counts.

    Division by zero gives 0.0 (nothing measured is not a pass), except `conf_over_08_ratio` where no triples means no
    over-confidence.
    """

    grounding_rate: float
    rk_macro_f1: float
    dir_acc: float
    task1_f1: float
    triples_per_chunk: float
    conf_over_08_ratio: float
    gold_triples_per_chunk: float
    causal_type_acc: float = 0.0
    n_chunks: int = 0
    n_gold_triples: int = 0
    n_pred_triples: int = 0
    n_matched: int = 0
    extract_cost_usd: float = 0.0


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def _f1(tp: int, fp: int, fn: int) -> float:
    return _ratio(2 * tp, 2 * tp + fp + fn)


def _macro_f1(pairs: Sequence[tuple[str, str]]) -> float:
    """Macro-F1 over the relation kinds that occur (in gold or prediction) among matched `(gold, pred)` pairs."""
    gold = Counter(g for g, _ in pairs)
    predicted = Counter(p for _, p in pairs)
    right = Counter(g for g, p in pairs if g == p)
    scores = [
        _f1(right[k], predicted[k] - right[k], gold[k] - right[k]) for k in REL_KINDS if gold[k] or predicted[k]
    ]
    return sum(scores) / len(scores) if scores else 0.0


def compute_metrics(items: Sequence[GoldItem], preds: Sequence[Prediction]) -> Metrics:
    """Metrics of `preds[i]` (the pipeline output for `items[i]`) over the whole gold set."""
    if len(items) != len(preds):
        raise PermanentError(f"regression: {len(items)} gold items but {len(preds)} predictions")
    rk_pairs: list[tuple[str, str]] = []
    n_dir = n_ct = n_gold = n_pred = n_high = n_emitted = n_ungrounded = 0
    tp = fp = fn = 0
    cost = 0.0
    for item, pred in zip(items, preds, strict=True):
        gold = item.labels.triples
        n_gold += len(gold)
        n_pred += len(pred.triples)
        n_high += sum(1 for t in pred.triples if t.confidence >= CONF_HIGH)
        n_emitted += pred.n_emitted
        n_ungrounded += pred.n_ungrounded
        cost += pred.cost_usd
        for i, j in match_triples(pred.triples, gold).pairs:
            p, g = pred.triples[i], gold[j]
            rk_pairs.append((g.relation_kind, p.relation_kind))
            n_dir += p.effect.direction == g.direction_effect
            n_ct += p.causal_type == g.causal_type
        predicted_causal = len(pred.triples) > 0  # W2-13: classify and at least one validated triple
        if predicted_causal and item.labels.has_causal:
            tp += 1
        elif predicted_causal:
            fp += 1
        elif item.labels.has_causal:
            fn += 1
    n = len(items)
    return Metrics(
        grounding_rate=_ratio(n_emitted - n_ungrounded, n_emitted),
        rk_macro_f1=_macro_f1(rk_pairs),
        dir_acc=_ratio(n_dir, len(rk_pairs)),
        task1_f1=_f1(tp, fp, fn),
        triples_per_chunk=_ratio(n_pred, n),
        conf_over_08_ratio=_ratio(n_high, n_pred),
        gold_triples_per_chunk=_ratio(n_gold, n),
        causal_type_acc=_ratio(n_ct, len(rk_pairs)),
        n_chunks=n,
        n_gold_triples=n_gold,
        n_pred_triples=n_pred,
        n_matched=len(rk_pairs),
        extract_cost_usd=cost,
    )


def failed_thresholds(m: Metrics) -> list[str]:
    """One line per tech section 7.2 threshold that `m` misses (none if all hold); each starts with the metric name."""
    out: list[str] = []
    for name, value, floor in (
        ("grounding_rate", m.grounding_rate, GROUNDING_MIN),
        ("rk_macro_f1", m.rk_macro_f1, RK_MACRO_F1_MIN),
        ("dir_acc", m.dir_acc, DIR_ACC_MIN),
        ("task1_f1", m.task1_f1, TASK1_F1_MIN),
    ):
        if value < floor:
            out.append(f"{name} {value:.4f} < {floor}")
    gold = m.gold_triples_per_chunk
    if abs(m.triples_per_chunk - gold) > TRIPLES_REL_TOL * gold + _EPS:
        out.append(
            f"triples_per_chunk {m.triples_per_chunk:.4f} outside gold {gold:.4f} +/-{TRIPLES_REL_TOL:.0%}"
        )
    if m.conf_over_08_ratio > CONF_OVER_08_MAX + _EPS:
        out.append(f"conf_over_08_ratio {m.conf_over_08_ratio:.4f} > {CONF_OVER_08_MAX}")
    return out


def assert_thresholds(m: Metrics) -> None:
    """Raise `AssertionError` listing every metric below its tech section 7.2 threshold (the CI gate)."""
    failed = failed_thresholds(m)
    if failed:
        raise AssertionError("golden thresholds missed:\n  " + "\n  ".join(failed))


@contextmanager
def _golden_budget() -> Iterator[None]:
    """Charge every `llm.call` made inside to `budget:golden:<date>` (W2-5) instead of the production budgets.

    `extract` and `classify` hard-code `budget:extract:`/`budget:classify:` keys; without this a gold run would eat
    the daily budget of the scheduled extraction.
    """
    inner = llm.call
    key = f"budget:golden:{report_date().isoformat()}"

    def call(**kwargs: Any) -> Any:
        kwargs["budget_key"] = key
        return inner(**kwargs)

    llm.call = call
    try:
        yield
    finally:
        llm.call = inner


def _chunk_and_source(item: GoldItem) -> tuple[Chunk, SourceSpec]:
    chunk = Chunk(
        id=item.chunk_id, doc_id=f"doc_{item.chunk_id}", idx=0, char_start=0, char_end=len(item.text),
        text=item.text, n_tokens=0,  # tokens are not counted for gold text
    )  # fmt: skip
    src = SourceSpec(
        id="src_gold", country="ZZ", region="NA", publisher=item.source.publisher, grade=item.source.grade,
        state_controlled=item.source.state_controlled, kind="rss", url="https://gold.invalid/", parser="RssCrawler",
        lang=item.lang,
    )  # fmt: skip
    return chunk, src


def _ungrounded_in_raw(raw: str | None, chunk: Chunk) -> tuple[int, int]:
    """(emitted, ungrounded) quotes of a failed extraction's last raw output; (0, 0) if it is not a valid result."""
    if raw is None:
        return 0, 0
    try:
        res = ExtractionResult.model_validate_json(raw)
    except (ValidationError, ValueError):
        return 0, 0
    return len(res.triples), sum(1 for t in res.triples if ground(t.quote, chunk.text) is None)


def _predict(item: GoldItem) -> Prediction:
    """The operating path of `extract_flow` for one chunk: classify, then extract with one reprompt (W2-13)."""
    chunk, src = _chunk_and_source(item)
    if not classify(chunk, item.lang):
        return Prediction([], 0, 0)
    try:
        out = extract(chunk, src, lang=item.lang, published_at=None)
    except ExtractFailed as e:
        emitted, ungrounded = _ungrounded_in_raw(e.raw, chunk)
        log.warning("gold_extract_failed", chunk_id=item.chunk_id, stage=e.stage)
        return Prediction([], emitted, ungrounded, e.cost_usd or 0.0)
    ungrounded = sum(1 for kind, _ in out.drops if kind == "grounding")
    return Prediction(out.triples, len(out.parsed.triples), ungrounded, out.cost_usd)


def run_extract_regression(gold_path: str | Path, model: str, prompt_version: str) -> Metrics:
    """Run the extraction pipeline (real LLM calls) over every gold chunk and score it against the labels.

    `model` and `prompt_version` name what is being measured; the extractor reads both from `settings.model_extract`
    and `PROMPT_VERSION`, so a mismatch is a `PermanentError` before any call rather than a silent measurement of
    something else. A budget, transient or configuration error aborts the run (a partial gold run is not a result).
    """
    items = load_gold(gold_path)  # a missing or malformed file fails here, before any LLM call
    if model != default_settings().model_extract:
        raise PermanentError(
            f"regression: model {model!r} is not settings.model_extract ({default_settings().model_extract!r}); "
            "set MODEL_EXTRACT to measure another model"
        )
    if prompt_version != PROMPT_VERSION:
        raise PermanentError(f"regression: prompt_version {prompt_version!r} is not the code's {PROMPT_VERSION!r}")
    with _golden_budget():
        preds = [_predict(item) for item in items]
    return compute_metrics(items, preds)
