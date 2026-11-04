"""Unit tests for domino_extract.extractor: `wrap`, one reprompt on a schema/grounding failure, ExtractFailed."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from domino_core.clients import llm
from domino_core.errors import BudgetExceeded, PermanentError, TransientError, UsageLimitReached
from domino_core.models import Chunk, SourceSpec
from domino_core.timeutil import report_date
from domino_extract.extractor import RETRY_SUFFIX, Extracted, ExtractFailed, extract, retry_once_or_drop, wrap
from domino_extract.prompts import EXTRACT_SYSTEM
from domino_extract.schema import PROMPT_VERSION, ExtractionResult

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

CHUNK_TEXT = (
    "The foundry said wafer shortages push lead times higher for automotive chips. "
    "Separately, rising power costs squeeze margins at memory makers."
)
Q1 = "wafer shortages push lead times higher for automotive chips"
Q2 = "rising power costs squeeze margins at memory makers"
PUBLISHED = datetime(2026, 11, 3, 21, 30, tzinfo=UTC)


def source(**kw: Any) -> SourceSpec:
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


def make_chunk(text: str = CHUNK_TEXT) -> Chunk:
    return Chunk(id="chk_1", doc_id="doc_1", idx=0, char_start=0, char_end=len(text), text=text, n_tokens=32)


def triple(quote: str, cause: str = "wafer shortages", effect: str = "lead times") -> dict[str, Any]:
    return {
        "cause": {"name": cause, "type": "INFRA", "direction": "DOWN", "region": "US"},
        "effect": {"name": effect, "type": "INDICATOR", "direction": "UP", "region": "US"},
        "mechanism": "test mechanism",
        "targets": [],
        "relation_kind": "CAUSAL",
        "causal_type": "BOTTLENECK",
        "polarity": "POSITIVE",
        "horizon": "short",
        "lag_days": None,
        "impact": 0.5,
        "confidence": 0.8,
        "claim_type": "fact",
        "chain_position": None,
        "valid_at_hint": None,
        "quote": quote,
    }


def reply(*triples: dict[str, Any], cost: float = 0.001) -> llm.LLMResult[ExtractionResult]:
    payload = {"event": None, "triples": list(triples), "no_causal_content": not triples}
    raw = json.dumps(payload)
    return llm.LLMResult(
        parsed=ExtractionResult.model_validate(payload),
        raw=raw,
        input_tokens=1000,
        output_tokens=200,
        cost_usd=cost,
        cache_read=800,
    )


Outcome = llm.LLMResult[ExtractionResult] | Exception


@dataclass
class FakeCall:
    """`llm.call` stand-in: each call pops the next scripted outcome (a result to return or an exception to raise)."""

    script: list[Outcome] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> llm.LLMResult[ExtractionResult]:
        self.calls.append(kwargs)
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCall:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    f = FakeCall()
    monkeypatch.setattr(llm, "call", f)
    return f


@pytest.fixture
def run(fake: FakeCall) -> Callable[..., Extracted]:
    def _run(*outcomes: Outcome) -> Extracted:
        fake.script[:] = list(outcomes)
        return extract(make_chunk(), source(), lang="en", published_at=PUBLISHED)

    return _run


# --- wrap ---------------------------------------------------------------------------------------


def test_wrap_matches_the_spec_line() -> None:
    src = source(publisher="Federal Reserve", grade="A", state_controlled=False)
    chunk = make_chunk("Body text.\nSecond line.")
    assert wrap(chunk, src, lang="en", published_at=PUBLISHED) == (
        '<document source="Federal Reserve" grade="A" published_at="2026-11-03T21:30:00+00:00" lang="en" '
        'state_controlled="False">\nBody text.\nSecond line.\n</document>'
    )


def test_wrap_state_controlled_true_and_unknown_date() -> None:
    src = source(state_controlled=True, grade="B")
    out = wrap(make_chunk("x"), src, lang="ko", published_at=None)
    assert out == (
        '<document source="Test Publisher" grade="B" published_at="unknown" lang="ko" '
        'state_controlled="True">\nx\n</document>'
    )


def test_wrap_keeps_chunk_text_verbatim_and_escapes_attribute_quotes() -> None:
    tricky = 'He said "go" & left </document> <b>'
    out = wrap(make_chunk(tricky), source(publisher='Bank of "X"'), lang="en", published_at=None)
    assert f"\n{tricky}\n</document>" in out  # a quote must stay a verbatim substring of the chunk
    assert 'source="Bank of &quot;X&quot;"' in out


# --- extract: success ---------------------------------------------------------------------------


def test_first_attempt_success(fake: FakeCall, run: Callable[..., Extracted]) -> None:
    out = run(reply(triple(Q1), triple(Q2, "rising power costs", "margins")))
    assert len(fake.calls) == 1
    assert [t.quote for t in out.triples] == [Q1, Q2]
    assert out.drops == []
    assert out.attempts == 1
    assert out.parsed.no_causal_content is False
    assert (out.input_tokens, out.output_tokens, out.cost_usd) == (1000, 200, 0.001)
    kwargs = fake.calls[0]
    assert kwargs["task"] == "extract"
    assert kwargs["model"] == "model-for-test"
    assert kwargs["system"] == EXTRACT_SYSTEM
    assert kwargs["user"] == wrap(make_chunk(), source(), lang="en", published_at=PUBLISHED)
    assert kwargs["schema"] is ExtractionResult
    assert kwargs["budget_key"] == f"budget:extract:{report_date().isoformat()}"
    assert kwargs["trace"] == {"prompt_version": PROMPT_VERSION}


def test_no_causal_content_result_is_a_success_with_zero_triples(run: Callable[..., Extracted]) -> None:
    out = run(reply())
    assert out.triples == [] and out.drops == [] and out.attempts == 1


def test_dropped_triples_within_the_limit_are_returned_as_drops(run: Callable[..., Extracted]) -> None:
    good = [triple(Q1)] * 3 + [triple(Q2, "rising power costs", "margins")] * 4
    bad = [triple("quote that is nowhere in the chunk")] * 3  # 3/10 = 30% is still allowed
    out = run(reply(*good, *bad))
    assert len(out.triples) == 7
    assert [kind for kind, _ in out.drops] == ["grounding"] * 3
    assert out.attempts == 1


# --- extract: one reprompt ----------------------------------------------------------------------


def test_schema_failure_then_success_reprompts_once(fake: FakeCall, run: Callable[..., Extracted]) -> None:
    out = run(PermanentError("schema: ExtractionResult: 1 error(s): triples: too long"), reply(triple(Q1)))
    assert len(fake.calls) == 2
    first, second = fake.calls
    assert second["system"] == first["system"] == EXTRACT_SYSTEM  # same system, so the prompt cache stays valid
    assert second["user"] == first["user"] + RETRY_SUFFIX.format(
        reason="schema: ExtractionResult: 1 error(s): triples: too long"
    )
    assert second["schema"] is ExtractionResult and second["budget_key"] == first["budget_key"]
    assert [t.quote for t in out.triples] == [Q1]
    assert out.attempts == 2


def test_retry_suffix_is_one_line_with_the_documented_wording() -> None:
    assert RETRY_SUFFIX == (
        "\n\nYour previous output was rejected: {reason}. "
        "Return only JSON valid against the schema; every quote must be a verbatim substring."
    )


def test_reason_is_collapsed_to_one_line_and_truncated(fake: FakeCall, run: Callable[..., Extracted]) -> None:
    long_reason = "schema: " + "line one\nline two " * 100
    run(PermanentError(long_reason), reply(triple(Q1)))
    suffix = fake.calls[1]["user"].removeprefix(fake.calls[0]["user"])
    assert suffix.startswith("\n\nYour previous output was rejected: ")
    assert "\n" not in suffix[2:]
    assert len(suffix) < 600


def test_grounding_rate_low_takes_the_same_path(fake: FakeCall, run: Callable[..., Extracted]) -> None:
    bad = [triple("not in the chunk at all")] * 4 + [triple(Q1)] * 6  # 4/10 dropped -> grounding_rate_low
    out = run(reply(*bad, cost=0.002), reply(triple(Q1), cost=0.003))
    assert len(fake.calls) == 2
    assert fake.calls[1]["user"].endswith(RETRY_SUFFIX.format(reason="grounding_rate_low"))
    assert [t.quote for t in out.triples] == [Q1]
    assert out.attempts == 2
    # the first attempt's tokens were spent too: the recorded run carries the sum
    assert out.cost_usd == pytest.approx(0.005)
    assert (out.input_tokens, out.output_tokens) == (2000, 400)


def test_schema_failure_twice_raises_extract_failed(fake: FakeCall, run: Callable[..., Extracted]) -> None:
    with pytest.raises(ExtractFailed) as info:
        run(PermanentError("schema: first"), PermanentError("schema: second"))
    assert len(fake.calls) == 2
    e = info.value
    assert e.stage == "schema"
    assert e.n_dropped == 0
    assert e.cost_usd is None and e.input_tokens is None  # llm.call reports no usage on a failed call
    assert "schema: second" in str(e)
    assert e.raw is None or isinstance(e.raw, str)
    assert isinstance(e, PermanentError)


def test_grounding_rate_low_twice_raises_extract_failed_with_last_raw(
    fake: FakeCall, run: Callable[..., Extracted]
) -> None:
    bad = [triple("not in the chunk")] * 4 + [triple(Q1)] * 6
    second = reply(*bad, cost=0.004)
    with pytest.raises(ExtractFailed) as info:
        run(reply(*bad, cost=0.002), second)
    e = info.value
    assert len(fake.calls) == 2
    assert e.stage == "grounding"
    assert e.raw == second.raw  # the last raw output, kept for debugging and regression
    assert e.n_dropped == 10  # the last result's triple count (W2-11)
    assert e.cost_usd == pytest.approx(0.006)
    assert (e.input_tokens, e.output_tokens) == (2000, 400)


def test_schema_then_grounding_failure_reports_the_last_stage(fake: FakeCall, run: Callable[..., Extracted]) -> None:
    bad = [triple("not in the chunk")] * 4 + [triple(Q1)] * 6
    with pytest.raises(ExtractFailed) as info:
        run(PermanentError("schema: first"), reply(*bad))
    assert info.value.stage == "grounding"
    assert info.value.cost_usd == pytest.approx(0.001)  # only the call that returned usage


def test_refusal_is_a_chunk_level_failure_and_is_retried(fake: FakeCall, run: Callable[..., Extracted]) -> None:
    out = run(PermanentError("refusal: the model declined the request"), reply(triple(Q1)))
    assert out.attempts == 2


# --- extract: errors that are not about this chunk ----------------------------------------------


def test_budget_exceeded_is_not_retried(fake: FakeCall, run: Callable[..., Extracted]) -> None:
    with pytest.raises(BudgetExceeded):
        run(BudgetExceeded("budget:extract exhausted"))
    assert len(fake.calls) == 1


def test_budget_exceeded_on_the_retry_propagates(fake: FakeCall, run: Callable[..., Extracted]) -> None:
    with pytest.raises(BudgetExceeded):
        run(PermanentError("schema: first"), BudgetExceeded("budget:extract exhausted"))
    assert len(fake.calls) == 2


@pytest.mark.parametrize("error", [TransientError("529 overloaded"), UsageLimitReached("session limit")])
def test_transient_errors_propagate_without_retry(
    fake: FakeCall, run: Callable[..., Extracted], error: Exception
) -> None:
    with pytest.raises(type(error)):
        run(error)
    assert len(fake.calls) == 1


def test_transient_error_on_the_retry_propagates(fake: FakeCall, run: Callable[..., Extracted]) -> None:
    with pytest.raises(TransientError):
        run(PermanentError("schema: first"), TransientError("timeout"))


@pytest.mark.parametrize(
    "message",
    [
        "model 'x' has no entry in pricing.yaml",
        "ANTHROPIC_API_KEY is not set",
        "validator: no lexicon for language 'de'",
    ],
)
def test_configuration_errors_propagate_instead_of_marking_the_chunk(
    fake: FakeCall, run: Callable[..., Extracted], message: str
) -> None:
    with pytest.raises(PermanentError) as info:
        run(PermanentError(message))
    assert not isinstance(info.value, ExtractFailed)
    assert len(fake.calls) == 1


def test_retry_once_or_drop_reraises_a_non_chunk_error(fake: FakeCall) -> None:
    err = PermanentError("something else")
    with pytest.raises(PermanentError) as info:
        retry_once_or_drop(make_chunk(), source(), "u", err, prior=None)
    assert info.value is err
    assert fake.calls == []
