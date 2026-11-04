"""domino_extract.store on Postgres: chunk selection, idempotent run records, one error row per drop."""

from __future__ import annotations

import hashlib
import json
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
from domino_core.errors import PermanentError
from domino_core.models import Chunk, Document, SourceSpec
from domino_extract import store
from domino_extract.extractor import Extracted, ExtractFailed
from domino_extract.schema import PROMPT_VERSION, CausalTriple, ExtractionResult
from domino_extract.validator import ValidatedTriple
from domino_ingest import store as ingest_store
from domino_ingest.sources import upsert_sources

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
            "REDIS_URL": "redis://unused",
            "S3_ENDPOINT": "http://unused",
            "S3_KEY": "unused",
            "S3_SECRET": "unused",
            "LANGFUSE_HOST": "http://unused",
            "LANGFUSE_PUBLIC_KEY": "unused",
            "LANGFUSE_SECRET_KEY": "unused",
            "RESEND_API_KEY": "unused",
            "FROM_EMAIL": "unused@example.com",
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


@pytest.fixture
def engine(migrated: Engine) -> Engine:
    with migrated.begin() as conn:
        conn.execute(
            text("TRUNCATE extraction_errors, extraction_runs, chunks, raw_documents, stories, sources CASCADE")
        )
    return migrated


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


def add_doc(engine: Engine, doc_id: str, source_id: str, lang: str, *, status: str = "chunked") -> None:
    doc = Document(
        id=doc_id,
        source_id=source_id,
        url=f"https://example.org/{doc_id}",
        url_hash=hashlib.sha256(doc_id.encode()).hexdigest(),
        title=None,
        published_at=PUBLISHED,
        lang=lang,
        doc_type="NEWS",
        text=f"text of {doc_id}",
        text_sha="0" * 64,
        simhash=0,
        snapshot_key=f"raw/{doc_id}.html",
        snapshot_sha="0" * 64,
    )
    ingest_store.insert_document(doc, status=status)


def add_chunk(
    engine: Engine, chunk_id: str, doc_id: str, idx: int = 0, *, status: str = "new", is_causal: bool | None = None
) -> None:
    body = f"body of {chunk_id}"
    chunk = Chunk(id=chunk_id, doc_id=doc_id, idx=idx, char_start=5, char_end=5 + len(body), text=body, n_tokens=4)
    ingest_store.insert_chunk(chunk)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE chunks SET status = :s, is_causal = :c WHERE id = :id"),
            {"s": status, "c": is_causal, "id": chunk_id},
        )


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).mappings().all())


def seed_mixed(engine: Engine) -> None:
    upsert_sources(
        [
            spec("src_en", "en"),
            spec("src_ko", "ko", country="KR", region="EA"),
            spec("src_zh", "zh"),
            spec("src_de", "de"),
        ],
        engine=engine,
    )
    add_doc(engine, "doc_en", "src_en", "en")
    add_doc(engine, "doc_ko", "src_ko", "ko")
    add_doc(engine, "doc_zh", "src_zh", "zh")
    add_doc(engine, "doc_de", "src_de", "de")
    add_doc(engine, "doc_new", "src_en", "en", status="new")  # not chunked yet
    add_doc(engine, "doc_skip", "src_en", "en", status="skipped")
    # inserted out of id order on purpose
    add_chunk(engine, "chk_05", "doc_ko")
    add_chunk(engine, "chk_01", "doc_en")
    add_chunk(engine, "chk_03", "doc_en", idx=1)
    add_chunk(engine, "chk_02", "doc_zh")  # zh: never selected (W2-10)
    add_chunk(engine, "chk_04", "doc_de")  # de: never selected
    add_chunk(engine, "chk_06", "doc_en", idx=2, status="extracted")
    add_chunk(engine, "chk_07", "doc_en", idx=3, status="dropped")
    add_chunk(engine, "chk_08", "doc_en", idx=4, status="error")
    add_chunk(engine, "chk_09", "doc_new", 0)  # document not chunked
    add_chunk(engine, "chk_10", "doc_skip", 0)  # document skipped


# --- new_chunks ---------------------------------------------------------------------------------


def test_new_chunks_selects_en_ko_new_chunks_of_chunked_documents_in_id_order(engine: Engine) -> None:
    seed_mixed(engine)
    pending = store.new_chunks()
    assert [p.chunk.id for p in pending] == ["chk_01", "chk_03", "chk_05"]
    # the unselected languages are left exactly as they were: status new, is_causal NULL (not dropped)
    left = rows(engine, "SELECT id, status, is_causal FROM chunks WHERE id IN ('chk_02', 'chk_04') ORDER BY id")
    assert [(r["id"], r["status"], r["is_causal"]) for r in left] == [("chk_02", "new", None), ("chk_04", "new", None)]


def test_new_chunks_carries_chunk_source_language_and_date(engine: Engine) -> None:
    seed_mixed(engine)
    first, _, ko = store.new_chunks()
    assert first.chunk == Chunk(
        id="chk_01",
        doc_id="doc_en",
        idx=0,
        char_start=5,
        char_end=5 + len("body of chk_01"),
        text="body of chk_01",
        n_tokens=4,
    )
    assert first.lang == "en" and ko.lang == "ko"
    assert first.published_at == PUBLISHED
    assert first.src.id == "src_en" and first.src.publisher == "Publisher src_en" and first.src.grade == "A"
    assert first.src.state_controlled is False
    assert ko.src.id == "src_ko" and ko.src.country == "KR"
    assert first.is_causal is None


def test_new_chunks_reports_an_existing_is_causal(engine: Engine) -> None:
    seed_mixed(engine)
    store.set_is_causal("chk_03", True)
    store.set_is_causal("chk_05", False)
    by_id = {p.chunk.id: p.is_causal for p in store.new_chunks()}
    assert by_id == {"chk_01": None, "chk_03": True, "chk_05": False}


def test_new_chunks_limit_takes_the_oldest_ids(engine: Engine) -> None:
    seed_mixed(engine)
    assert [p.chunk.id for p in store.new_chunks(limit=2)] == ["chk_01", "chk_03"]
    assert store.new_chunks(limit=0) == []


def test_new_chunks_empty_database(engine: Engine) -> None:
    assert store.new_chunks() == []


# --- small writes -------------------------------------------------------------------------------


def test_set_is_causal_and_set_chunk_status(engine: Engine) -> None:
    seed_mixed(engine)
    store.set_is_causal("chk_01", True)
    store.set_chunk_status("chk_01", "extracted")
    store.set_chunk_status("chk_03", "dropped")
    got = {r["id"]: (r["is_causal"], r["status"]) for r in rows(engine, "SELECT id, is_causal, status FROM chunks")}
    assert got["chk_01"] == (True, "extracted")
    assert got["chk_03"] == (None, "dropped")
    assert [p.chunk.id for p in store.new_chunks()] == ["chk_05"]  # extracted/dropped chunks leave the queue


def test_set_chunk_status_rejects_an_unknown_status(engine: Engine) -> None:
    seed_mixed(engine)
    with pytest.raises(PermanentError, match="status"):
        store.set_chunk_status("chk_01", "finished")


# --- records ------------------------------------------------------------------------------------


def make_triple(quote: str = "q", cause: str = "a", effect: str = "b") -> CausalTriple:
    return CausalTriple.model_validate(
        {
            "cause": {"name": cause, "type": "INFRA", "direction": "DOWN", "region": "US"},
            "effect": {"name": effect, "type": "INDICATOR", "direction": "UP", "region": "US"},
            "mechanism": "m",
            "relation_kind": "CAUSAL",
            "causal_type": "DIRECT",
            "polarity": "POSITIVE",
            "horizon": "short",
            "impact": 0.5,
            "confidence": 0.8,
            "claim_type": "fact",
            "quote": quote,
        }
    )


def make_extracted(*, n_ok: int = 1, drops: list[tuple[str, CausalTriple]] | None = None) -> Extracted:
    ok = [
        ValidatedTriple(
            **make_triple(f"quote {i}").model_dump(),
            chunk_id="chk_01",
            char_start=5,
            char_end=12,
            is_correlation_only=False,
        )
        for i in range(n_ok)
    ]
    payload = {
        "event": None,
        "triples": [make_triple(f"quote {i}").model_dump() for i in range(n_ok)],
        "no_causal_content": n_ok == 0,
    }
    return Extracted(
        triples=ok,
        drops=drops or [],  # type: ignore[arg-type]
        parsed=ExtractionResult.model_validate(payload),
        raw=json.dumps(payload),
        input_tokens=1200,
        output_tokens=150,
        cost_usd=0.00123,
        cache_read=900,
        attempts=1,
    )


def runs(engine: Engine) -> list[Any]:
    return rows(engine, "SELECT * FROM extraction_runs ORDER BY created_at, id")


def test_record_run_stores_the_extraction(engine: Engine) -> None:
    seed_mixed(engine)
    outcome = make_extracted(n_ok=2, drops=[("grounding", make_triple("lost"))])
    run_id = store.record_run("chk_01", outcome, model=MODEL)
    assert run_id is not None and run_id.startswith("run_")
    (row,) = runs(engine)
    assert row["id"] == run_id and row["chunk_id"] == "chk_01"
    assert row["model"] == MODEL and row["prompt_version"] == PROMPT_VERSION == "ext-1.0"
    assert (row["input_tokens"], row["output_tokens"]) == (1200, 150)
    assert float(row["cost_usd"]) == pytest.approx(0.00123)
    assert (row["n_triples"], row["n_dropped"]) == (2, 1)
    assert row["raw_json"] == json.loads(outcome.raw)  # the model's raw output, jsonb


def test_record_run_twice_leaves_one_row(engine: Engine) -> None:
    seed_mixed(engine)
    first = store.record_run("chk_01", make_extracted(), model=MODEL)
    second = store.record_run("chk_01", make_extracted(n_ok=3), model=MODEL)
    assert first is not None
    assert second is None  # `(chunk_id, prompt_version)` already recorded: nothing written
    (row,) = runs(engine)
    assert row["id"] == first and row["n_triples"] == 1  # the first record stands


def test_a_new_prompt_version_is_a_new_run(engine: Engine) -> None:
    seed_mixed(engine)
    assert store.record_run("chk_01", make_extracted(), model=MODEL, prompt_version="ext-1.0") is not None
    assert store.record_run("chk_01", make_extracted(), model=MODEL, prompt_version="ext-1.1") is not None
    assert len(runs(engine)) == 2
    assert store.run_exists("chk_01", "ext-1.0") and store.run_exists("chk_01", "ext-1.1")


def test_run_exists(engine: Engine) -> None:
    seed_mixed(engine)
    assert store.run_exists("chk_01", PROMPT_VERSION) is False
    store.record_run("chk_01", make_extracted(), model=MODEL)
    assert store.run_exists("chk_01", PROMPT_VERSION) is True
    assert store.run_exists("chk_01", "ext-9.9") is False
    assert store.run_exists("chk_03", PROMPT_VERSION) is False


def test_record_run_for_a_failed_extraction(engine: Engine) -> None:
    seed_mixed(engine)
    raw = json.dumps({"event": None, "triples": [], "no_causal_content": False})
    failed = ExtractFailed(
        "grounding",
        raw,
        reason="grounding_rate_low",
        n_dropped=10,
        input_tokens=2000,
        output_tokens=400,
        cost_usd=0.005,
    )
    store.record_run("chk_01", failed, model=MODEL)
    (row,) = runs(engine)
    assert (row["n_triples"], row["n_dropped"]) == (0, 10)
    assert row["raw_json"] == json.loads(raw)
    assert (row["input_tokens"], row["output_tokens"]) == (2000, 400)
    assert float(row["cost_usd"]) == pytest.approx(0.005)
    assert store.run_exists("chk_01", PROMPT_VERSION)  # a failed chunk is not retried under the same prompt version


def test_record_run_for_a_failure_without_raw_output_or_usage(engine: Engine) -> None:
    seed_mixed(engine)
    store.record_run("chk_01", ExtractFailed("schema", None, reason="schema: ExtractionResult: bad"), model=MODEL)
    store.record_run("chk_03", ExtractFailed("schema", "not json {", reason="schema: x"), model=MODEL)
    by_chunk = {r["chunk_id"]: r for r in runs(engine)}
    assert by_chunk["chk_01"]["raw_json"] == {"raw": None, "error": "schema: ExtractionResult: bad"}
    assert by_chunk["chk_03"]["raw_json"] == {"raw": "not json {"}
    assert by_chunk["chk_01"]["input_tokens"] is None and by_chunk["chk_01"]["cost_usd"] is None
    assert by_chunk["chk_01"]["n_triples"] == 0 and by_chunk["chk_01"]["n_dropped"] == 0


def errors(engine: Engine) -> list[Any]:
    return rows(engine, "SELECT * FROM extraction_errors ORDER BY id")


def test_one_error_row_per_drop(engine: Engine) -> None:
    seed_mixed(engine)
    drops = [
        ("grounding", make_triple("lost one")),
        ("self_ref", make_triple("same", "x", "x")),
        ("grounding", make_triple("lost two")),
    ]
    outcome = make_extracted(n_ok=7, drops=drops)
    run_id = store.record_run("chk_01", outcome, model=MODEL)
    assert run_id is not None
    store.record_errors("chk_01", run_id, outcome)
    got = errors(engine)
    assert [(r["stage"], r["reason"]) for r in got] == [
        ("grounding", "grounding"),
        ("validator", "self_ref"),
        ("grounding", "grounding"),
    ]
    assert all(r["chunk_id"] == "chk_01" and r["run_id"] == run_id for r in got)
    assert [r["payload"]["quote"] for r in got] == ["lost one", "same", "lost two"]


def test_no_error_rows_when_nothing_was_dropped(engine: Engine) -> None:
    seed_mixed(engine)
    outcome = make_extracted()
    run_id = store.record_run("chk_01", outcome, model=MODEL)
    assert run_id is not None
    store.record_errors("chk_01", run_id, outcome)
    assert errors(engine) == []


def test_extract_failed_gets_one_error_row_with_its_stage(engine: Engine) -> None:
    seed_mixed(engine)
    failed = ExtractFailed("schema", None, reason="schema: ExtractionResult: 1 error(s): triples: too long")
    run_id = store.record_run("chk_01", failed, model=MODEL)
    assert run_id is not None
    store.record_errors("chk_01", run_id, failed)
    (row,) = errors(engine)
    assert (row["stage"], row["chunk_id"], row["run_id"]) == ("schema", "chk_01", run_id)
    assert row["reason"] == "schema: ExtractionResult: 1 error(s): triples: too long"


def test_run_and_errors_commit_together_through_a_shared_connection(engine: Engine) -> None:
    seed_mixed(engine)
    outcome = make_extracted(drops=[("grounding", make_triple("lost"))])
    with pytest.raises(RuntimeError), db_client.connection() as conn:
        run_id = store.record_run("chk_01", outcome, model=MODEL, conn=conn)
        assert run_id is not None
        store.record_errors("chk_01", run_id, outcome, conn=conn)
        store.set_chunk_status("chk_01", "extracted", conn=conn)
        raise RuntimeError("crash before commit")
    assert runs(engine) == [] and errors(engine) == []
    assert next(p.chunk.id for p in store.new_chunks()) == "chk_01"  # status change rolled back too
