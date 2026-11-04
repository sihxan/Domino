"""Unit tests for domino_extract.classifier: keyword prefilter, conservative pass on LLM failure, budget propagation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from domino_core.clients import llm
from domino_core.errors import BudgetExceeded, PermanentError, TransientError, UsageLimitReached
from domino_core.models import Chunk
from domino_core.timeutil import report_date
from domino_extract.classifier import CAUSAL_HINTS, CLASSIFY_SYSTEM, EXTRACT_LANGS, classify

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

EN_HINTED = "Wafer shortages push lead times higher for automotive chips."
EN_PLAIN = "The committee will meet again in December to review the data."
KO_HINTED = "환율 상승 때문에 수입물가가 올랐다."
KO_PLAIN = "위원회는 12월에 다시 회의를 열 예정이다."


@dataclass
class FakeCall:
    """Stands in for `llm.call`: records kwargs, returns `raw` or raises `error`."""

    raw: str = "Y"
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    def __call__(self, **kwargs: Any) -> llm.LLMResult[str]:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return llm.LLMResult(
            parsed=self.raw, raw=self.raw, input_tokens=10, output_tokens=1, cost_usd=0.0001, cache_read=0
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeCall:
    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    f = FakeCall()
    monkeypatch.setattr(llm, "call", f)
    return f


def chunk(text: str) -> Chunk:
    return Chunk(id="chk_1", doc_id="doc_1", idx=0, char_start=0, char_end=len(text), text=text, n_tokens=16)


def test_no_hint_means_no_llm_call_and_false(fake: FakeCall) -> None:
    assert classify(chunk(EN_PLAIN), "en") is False
    assert classify(chunk(KO_PLAIN), "ko") is False
    assert fake.calls == []


def test_hint_and_y_is_true(fake: FakeCall) -> None:
    assert classify(chunk(EN_HINTED), "en") is True
    assert classify(chunk(KO_HINTED), "ko") is True
    assert len(fake.calls) == 2


def test_hint_match_is_case_insensitive(fake: FakeCall) -> None:
    assert classify(chunk("Prices rose DUE TO weak supply."), "en") is True
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    ("raw", "expected"), [("Y", True), (" y\n", True), ("Yes", True), ("N", False), ("no", False), ("", False)]
)
def test_answer_parsing(fake: FakeCall, raw: str, expected: bool) -> None:
    fake.raw = raw
    assert classify(chunk(EN_HINTED), "en") is expected


def test_call_arguments(fake: FakeCall) -> None:
    classify(chunk(EN_HINTED), "en")
    (kwargs,) = fake.calls
    assert kwargs["task"] == "classify"
    assert kwargs["model"] == "model-for-test"  # from settings.model_extract, never a literal
    assert kwargs["system"] == CLASSIFY_SYSTEM
    assert kwargs["user"] == EN_HINTED
    assert kwargs["max_tokens"] == 2
    assert kwargs["budget_key"] == f"budget:classify:{report_date().isoformat()}"


def test_classify_system_text() -> None:
    assert CLASSIFY_SYSTEM == (
        "Answer Y if the text asserts or reports that one thing causes, drives, enables, constrains or replaces "
        "another (any language). Otherwise N. Answer with one letter."
    )


@pytest.mark.parametrize("error", [TransientError("429"), PermanentError("400")])
def test_llm_failure_passes_conservatively(fake: FakeCall, error: Exception) -> None:
    fake.error = error
    assert classify(chunk(EN_HINTED), "en") is True
    assert len(fake.calls) == 1


def test_budget_exceeded_propagates(fake: FakeCall) -> None:
    fake.error = BudgetExceeded("budget:classify exhausted")  # a PermanentError subclass, but it must not pass
    with pytest.raises(BudgetExceeded):
        classify(chunk(EN_HINTED), "en")


def test_usage_limit_propagates(fake: FakeCall) -> None:
    # Code §0.4: on a subscription limit the caller stops the run's LLM stage — passing the chunk on would only
    # send it to the extract call, which hits the same limit.
    fake.error = UsageLimitReached("session limit", limit_type="five_hour")
    with pytest.raises(UsageLimitReached):
        classify(chunk(EN_HINTED), "en")


def test_unsupported_language_is_an_error_not_a_silent_false(fake: FakeCall) -> None:
    with pytest.raises(PermanentError):
        classify(chunk(EN_HINTED), "de")
    assert fake.calls == []


def test_extract_langs_follow_the_lexicons() -> None:
    assert set(EXTRACT_LANGS) == {"en", "ko"}
    assert set(CAUSAL_HINTS) == {"en", "ko"}
