"""flows.extract_flow (and the ingest trigger) against a Postgres container; `llm.call` is a scripted fake (T13).

No real LLM call happens anywhere here: classifier and extractor both go through `domino_core.clients.llm.call`,
which every test replaces. The switch `EXTRACT_ENABLED` (default off) is part of what is tested.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer

from domino_core.clients import db as db_client
from domino_core.clients import llm
from domino_core.errors import BudgetExceeded, PermanentError, TransientError, UsageLimitReached
from domino_core.models import Chunk, Document, SourceSpec
from domino_core.settings import get_settings
from domino_extract.schema import PROMPT_VERSION, ExtractionResult
from domino_ingest import store as ingest_store
from domino_ingest.sources import upsert_sources
from flows import ingest_flow as ingest_module
from flows.extract_flow import extract_flow
from flows.ingest_flow import ingest_flow

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
PUBLISHED = datetime(2026, 11, 3, 21, 0, tzinfo=UTC)
MODEL = "model-for-test"


@pytest.fixture(scope="module")
def migrated() -> Iterator[Engine]:
    with PostgresContainer("postgres:16.15", driver="psycopg") as pg:
        url = pg.get_connection_url()
        mp = pytest.MonkeyPatch()
        env = {
            "PG_DSN": url,
            "NEO4J_URI": "bolt://unused",
            "NEO4J_USER": "unused",
            "NEO4J_PASSWORD": "unused",
            "REDIS_URL": "redis://unused",  # llm.call is faked: nothing here touches Redis
            "S3_ENDPOINT": "http://unused",
            "S3_KEY": "unused",
            "S3_SECRET": "unused",
            "LANGFUSE_HOST": "http://unused",
            "LANGFUSE_PUBLIC_KEY": "unused",
            "LANGFUSE_SECRET_KEY": "unused",
            "RESEND_API_KEY": "unused",
            "FROM_EMAIL": "unused@example.com",
            "MODEL_EXTRACT": MODEL,
        }
        for k, v in env.items():
            mp.setenv(k, v)
        command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
        mp.setattr(db_client, "_engine", None)  # the default engine is a process-wide singleton: point it here
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()
            db_client.get_engine().dispose()
            mp.undo()


# --- the fake LLM -------------------------------------------------------------------------------

EN_QUOTE = "Shortage {tag} because of fab delays"
KO_QUOTE = "{tag} 부품 부족 때문에 납기가 길어졌다"


def en_text(tag: str) -> str:
    return f"{EN_QUOTE.format(tag=tag)} pushes lead times higher for car chips."


def ko_text(tag: str) -> str:
    return f"{KO_QUOTE.format(tag=tag)}."


def hintless_text(tag: str) -> str:
    return f"The committee met on Tuesday ({tag}) and published its minutes."


def zh_text(tag: str) -> str:
    return f"供应短缺 {tag} 导致交货期延长。"


def triple(quote: str, i: int = 0) -> dict[str, Any]:
    return {
        "cause": {"name": f"cause {i}", "type": "INFRA", "direction": "DOWN", "region": "US"},
        "effect": {"name": f"effect {i}", "type": "INDICATOR", "direction": "UP", "region": "US"},
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


class FakeLLM:
    """Replaces `llm.call`. The chunk is recognised by its tag (`c1`...) in the prompt text.

    `script[(task, tag)]` is a list of errors raised by the next calls for that chunk and task (then it succeeds).
    `shape[tag] = (n_good, n_bad)` sets how many grounded / ungrounded triples an extraction returns.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: dict[tuple[str, str], list[Exception]] = {}
        self.shape: dict[str, tuple[int, int]] = {}
        self.classify_answer = "Y"

    def of(self, task: str, tag: str | None = None) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["task"] == task and (tag is None or c["tag"] == tag)]

    def __call__(self, **kw: Any) -> llm.LLMResult[Any]:
        tag = re.search(r"\bc\d+\b", kw["user"]).group(0)  # type: ignore[union-attr]
        self.calls.append({**kw, "tag": tag})
        pending = self.script.get((kw["task"], tag))
        if pending:
            raise pending.pop(0)
        if kw["task"] == "classify":
            a = self.classify_answer
            return llm.LLMResult(parsed=a, raw=a, input_tokens=40, output_tokens=1, cost_usd=0.0001, cache_read=0)
        quote = (KO_QUOTE if "부족" in kw["user"] else EN_QUOTE).format(tag=tag)
        n_good, n_bad = self.shape.get(tag, (1, 0))
        triples = [triple(quote, i) for i in range(n_good)] + [
            triple(f"never written anywhere {i}", n_good + i) for i in range(n_bad)
        ]
        payload = {"event": None, "triples": triples, "no_causal_content": not triples}
        return llm.LLMResult(
            parsed=ExtractionResult.model_validate(payload),
            raw=json.dumps(payload),
            input_tokens=900,
            output_tokens=120,
            cost_usd=0.0007,
            cache_read=700,
        )


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeLLM:
    f = FakeLLM()
    monkeypatch.setattr(llm, "call", f)
    return f


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXTRACT_ENABLED", "true")


@pytest.fixture
def engine(migrated: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    monkeypatch.delenv("EXTRACT_ENABLED", raising=False)  # tests that need the switch on say so
    with migrated.begin() as conn:
        conn.execute(
            text("TRUNCATE extraction_errors, extraction_runs, chunks, raw_documents, stories, sources CASCADE")
        )
    return migrated


# --- seeding ------------------------------------------------------------------------------------


def spec(source_id: str, lang: str, **kw: Any) -> SourceSpec:
    base: dict[str, Any] = {
        "id": source_id,
        "country": "US",
        "region": "NA",
        "publisher": f"Publisher {source_id}",
        "grade": "A",
        "kind": "rss",
        "url": f"https://{source_id}.example.org/feed.xml",
        "parser": "RssCrawler",
        "lang": lang,
    }
    return SourceSpec(**(base | kw))


def add_chunk(engine: Engine, tag: str, lang: str, body: str) -> None:
    """One source, one document and one chunk (`chk_<tag>`, document `doc_<tag>`)."""
    source_id = f"src_{lang}"
    upsert_sources([spec(source_id, lang)], engine=engine)
    doc = Document(
        id=f"doc_{tag}",
        source_id=source_id,
        url=f"https://example.org/{tag}",
        url_hash=hashlib.sha256(tag.encode()).hexdigest(),
        title=None,
        published_at=PUBLISHED,
        lang=lang,
        doc_type="NEWS",
        text=body,
        text_sha="0" * 64,
        simhash=0,
        snapshot_key=f"raw/{tag}.html",
        snapshot_sha="0" * 64,
    )
    ingest_store.insert_document(doc, status="chunked")
    ingest_store.insert_chunk(
        Chunk(id=f"chk_{tag}", doc_id=f"doc_{tag}", idx=0, char_start=0, char_end=len(body), text=body, n_tokens=20)
    )


def seed_three_en(engine: Engine) -> None:
    for tag in ("c1", "c2", "c3"):
        add_chunk(engine, tag, "en", en_text(tag))


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).mappings().all())


def chunk_state(engine: Engine) -> dict[str, tuple[str, bool | None]]:
    return {
        r["id"].removeprefix("chk_"): (r["status"], r["is_causal"])
        for r in rows(engine, "SELECT id, status, is_causal FROM chunks")
    }


def run_flow() -> None:
    extract_flow.fn()


# --- one run ------------------------------------------------------------------------------------


def test_first_run_records_runs_and_marks_chunks(engine: Engine, fake: FakeLLM, enabled: None) -> None:
    add_chunk(engine, "c1", "en", en_text("c1"))
    add_chunk(engine, "c2", "en", en_text("c2"))
    add_chunk(engine, "c3", "ko", ko_text("c3"))
    run_flow()
    assert chunk_state(engine) == {"c1": ("extracted", True), "c2": ("extracted", True), "c3": ("extracted", True)}
    runs = {r["chunk_id"]: r for r in rows(engine, "SELECT * FROM extraction_runs")}
    assert set(runs) == {"chk_c1", "chk_c2", "chk_c3"}
    for r in runs.values():
        assert r["prompt_version"] == PROMPT_VERSION == "ext-1.0"
        assert r["model"] == MODEL  # from settings, not a literal
        assert (r["n_triples"], r["n_dropped"]) == (1, 0)
        assert (r["input_tokens"], r["output_tokens"]) == (900, 120)
    assert rows(engine, "SELECT 1 FROM extraction_errors") == []
    # classify once and extract once per chunk, nothing else
    assert len(fake.of("classify")) == 3 and len(fake.of("extract")) == 3
    first = fake.of("extract", "c1")[0]
    assert first["model"] == MODEL and first["trace"] == {"prompt_version": PROMPT_VERSION}
    assert first["budget_key"].startswith("budget:extract:")


def test_dropped_triples_leave_error_rows_but_the_chunk_is_extracted(
    engine: Engine, fake: FakeLLM, enabled: None
) -> None:
    add_chunk(engine, "c2", "en", en_text("c2"))
    fake.shape["c2"] = (3, 1)  # 1 of 4 ungrounded = 25%, under the 30% limit
    run_flow()
    assert chunk_state(engine) == {"c2": ("extracted", True)}
    (run,) = rows(engine, "SELECT * FROM extraction_runs")
    assert (run["n_triples"], run["n_dropped"]) == (3, 1)
    (err,) = rows(engine, "SELECT * FROM extraction_errors")
    assert (err["stage"], err["reason"], err["run_id"]) == ("grounding", "grounding", run["id"])


def test_second_run_changes_nothing_and_calls_no_llm(engine: Engine, fake: FakeLLM, enabled: None) -> None:
    seed_three_en(engine)
    add_chunk(engine, "c4", "en", hintless_text("c4"))
    run_flow()
    before = (
        rows(engine, "SELECT id, chunk_id, created_at FROM extraction_runs ORDER BY id"),
        chunk_state(engine),
        len(fake.calls),
    )
    run_flow()
    after = (
        rows(engine, "SELECT id, chunk_id, created_at FROM extraction_runs ORDER BY id"),
        chunk_state(engine),
        len(fake.calls),
    )
    assert after == before
    assert len(after[0]) == 3


def test_chunk_without_a_hint_is_dropped_without_an_llm_call(engine: Engine, fake: FakeLLM, enabled: None) -> None:
    add_chunk(engine, "c4", "en", hintless_text("c4"))
    run_flow()
    assert chunk_state(engine) == {"c4": ("dropped", False)}
    assert fake.calls == []
    assert rows(engine, "SELECT 1 FROM extraction_runs") == []


def test_classifier_no_leaves_a_dropped_chunk_with_is_causal_false(
    engine: Engine, fake: FakeLLM, enabled: None
) -> None:
    add_chunk(engine, "c1", "en", en_text("c1"))  # has hint words, but the model answers N
    fake.classify_answer = "N"
    run_flow()
    assert chunk_state(engine) == {"c1": ("dropped", False)}
    assert fake.of("extract") == []


def test_unsupported_language_is_never_selected(engine: Engine, fake: FakeLLM, enabled: None) -> None:
    add_chunk(engine, "c5", "zh", zh_text("c5"))
    run_flow()
    assert chunk_state(engine) == {"c5": ("new", None)}  # untouched: neither dropped nor classified
    assert fake.calls == []
    assert rows(engine, "SELECT 1 FROM extraction_runs") == []


# --- stopping -----------------------------------------------------------------------------------


@pytest.mark.parametrize("task", ["classify", "extract"])
def test_budget_exceeded_ends_the_run_and_leaves_the_rest_new(
    engine: Engine, fake: FakeLLM, enabled: None, task: str
) -> None:
    seed_three_en(engine)
    fake.script[(task, "c2")] = [BudgetExceeded("budget:extract:today")]
    run_flow()
    state = chunk_state(engine)
    assert state["c1"] == ("extracted", True)
    assert state["c2"][0] == "new" and state["c3"] == ("new", None)  # c3 was never looked at
    assert [r["chunk_id"] for r in rows(engine, "SELECT chunk_id FROM extraction_runs")] == ["chk_c1"]
    assert fake.of("classify", "c3") == [] and fake.of("extract", "c3") == []
    assert rows(engine, "SELECT 1 FROM extraction_errors") == []


# A plain TransientError from the classifier passes the chunk on as "causal" (tech section 5), so it cannot stop a run.
@pytest.mark.parametrize(
    ("task", "error"),
    [
        ("extract", TransientError("503")),
        ("extract", UsageLimitReached("session limit")),
        ("classify", UsageLimitReached("session limit")),
    ],
)
def test_transient_error_ends_the_run_and_leaves_that_chunk_alone(
    engine: Engine, fake: FakeLLM, enabled: None, task: str, error: TransientError
) -> None:
    seed_three_en(engine)
    fake.script[(task, "c2")] = [error]
    run_flow()
    state = chunk_state(engine)
    assert state["c1"] == ("extracted", True)
    assert state["c2"][0] == "new" and state["c3"] == ("new", None)
    assert [r["chunk_id"] for r in rows(engine, "SELECT chunk_id FROM extraction_runs")] == ["chk_c1"]
    assert fake.of("classify", "c3") == [] and fake.of("extract", "c3") == []
    assert rows(engine, "SELECT 1 FROM extraction_errors") == []


def test_the_next_run_continues_where_a_transient_stop_left_off(engine: Engine, fake: FakeLLM, enabled: None) -> None:
    seed_three_en(engine)
    fake.script[("extract", "c2")] = [TransientError("503")]
    run_flow()
    run_flow()  # the error is spent: c2 is extracted now, c1 is not extracted twice
    assert {k: v[0] for k, v in chunk_state(engine).items()} == {
        "c1": "extracted",
        "c2": "extracted",
        "c3": "extracted",
    }
    assert len(rows(engine, "SELECT 1 FROM extraction_runs")) == 3
    assert len(fake.of("classify", "c1")) == 1 and len(fake.of("extract", "c1")) == 1
    assert len(fake.of("classify", "c2")) == 1  # the classification was kept (`is_causal`), only extract was repeated
    assert len(fake.of("extract", "c2")) == 2


def test_a_chunk_that_fails_twice_is_marked_error_and_the_run_goes_on(
    engine: Engine, fake: FakeLLM, enabled: None
) -> None:
    seed_three_en(engine)
    fake.script[("extract", "c2")] = [PermanentError("schema: bad json"), PermanentError("schema: still bad")]
    run_flow()
    assert {k: v[0] for k, v in chunk_state(engine).items()} == {"c1": "extracted", "c2": "error", "c3": "extracted"}
    runs = {r["chunk_id"]: r for r in rows(engine, "SELECT * FROM extraction_runs")}
    assert runs["chk_c2"]["n_triples"] == 0
    (err,) = rows(engine, "SELECT * FROM extraction_errors")
    assert (err["chunk_id"], err["stage"], err["run_id"]) == ("chk_c2", "schema", runs["chk_c2"]["id"])
    assert len(fake.of("extract", "c2")) == 2  # one reprompt
    run_flow()  # an error chunk is not tried again under the same prompt version
    assert len(fake.of("extract", "c2")) == 2


def test_a_failure_that_is_not_about_the_output_stops_the_flow_loudly(
    engine: Engine, fake: FakeLLM, enabled: None
) -> None:
    seed_three_en(engine)
    fake.script[("extract", "c2")] = [PermanentError("model 'x' has no entry in pricing.yaml")]
    with pytest.raises(PermanentError, match=r"pricing\.yaml"):
        run_flow()
    state = chunk_state(engine)
    assert state["c1"][0] == "extracted" and state["c2"][0] == "new"  # not turned into an `error` chunk (T12)


# --- the switch ---------------------------------------------------------------------------------


def test_extract_enabled_defaults_to_off(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    assert get_settings().extract_enabled is False
    get_settings.cache_clear()
    monkeypatch.setenv("EXTRACT_ENABLED", "true")
    assert get_settings().extract_enabled is True


@pytest.mark.parametrize("value", [None, "false", ""])
def test_disabled_extract_flow_does_nothing(
    engine: Engine,
    fake: FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str | None,
) -> None:
    seed_three_en(engine)
    add_chunk(engine, "c4", "en", hintless_text("c4"))
    if value is not None:
        monkeypatch.setenv("EXTRACT_ENABLED", value)
    before = chunk_state(engine)
    run_flow()
    assert fake.calls == []  # not even the free keyword pre-filter's LLM-less path decided anything
    assert chunk_state(engine) == before  # c4 is still `new`, not `dropped`
    assert rows(engine, "SELECT 1 FROM extraction_runs") == []
    assert rows(engine, "SELECT 1 FROM extraction_errors") == []
    logged = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert [rec["event"] for rec in logged if rec["event"].startswith("extract_")] == ["extract_disabled"]


# --- the ingest trigger -------------------------------------------------------------------------


@pytest.fixture
def triggers(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[Any, ...], dict[str, Any]]]:
    seen: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def fake_run_deployment(*args: Any, **kwargs: Any) -> None:
        seen.append((args, kwargs))

    monkeypatch.setattr(ingest_module, "run_deployment", fake_run_deployment)
    return seen


def run_ingest() -> None:
    ingest_flow.fn(now=datetime(2026, 11, 4, 3, 0, tzinfo=UTC))  # no sources are due: nothing to fetch


def test_ingest_does_not_trigger_extract_while_disabled(
    engine: Engine,
    triggers: list[tuple[tuple[Any, ...], dict[str, Any]]],
    fake: FakeLLM,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_ingest()
    assert triggers == []
    assert fake.calls == []
    logged = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert [rec["event"] for rec in logged if rec["event"].startswith("extract_")] == ["extract_trigger_skipped"]


def test_ingest_triggers_the_extract_deployment_without_waiting_when_enabled(
    engine: Engine, triggers: list[tuple[tuple[Any, ...], dict[str, Any]]], enabled: None
) -> None:
    run_ingest()
    assert triggers == [(("extract/extract",), {"timeout": 0})]  # W2-12: fire and forget


def test_a_failed_trigger_does_not_fail_ingest(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    enabled: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def no_deployment(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError(
            "deployment 'extract/extract' not found"
        )  # e.g. a manual run on a host without `make deploy`

    monkeypatch.setattr(ingest_module, "run_deployment", no_deployment)
    run_ingest()  # must not raise
    logged = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line.startswith("{")]
    assert "extract_trigger_failed" in [rec["event"] for rec in logged]
