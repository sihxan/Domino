"""store + ingest_flow against Postgres and minio containers; crawlers are fakes or the real ones on fake HTTP (T11)."""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine, text
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import DockerContainer

from domino_core.clients import db as db_client
from domino_core.clients import http
from domino_core.clients import s3 as s3_client
from domino_core.clients.db import connection
from domino_core.errors import PermanentError, TransientError
from domino_core.models import Chunk, Document, RawDoc, SourceSpec
from domino_core.settings import Settings, get_settings
from domino_ingest import store
from domino_ingest.crawlers import CRAWLERS, base
from domino_ingest.crawlers.base import Clock
from domino_ingest.normalizer import LangUndetectedError, TooShortError, url_hash
from domino_ingest.simhash import to_signed
from domino_ingest.sources import upsert_sources
from flows import ingest_flow as ingest_module
from flows.ingest_flow import ingest_flow

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures" / "ingest"
MINIO_IMAGE = "bitnamilegacy/minio:2025.7.23-debian-12-r5"  # same image as docker-compose.yml
MINIO_USER, MINIO_PASSWORD, BUCKET = "testkey", "testsecret123", "domino"  # throwaway container

T0 = datetime(2026, 11, 4, 3, 0, tzinfo=UTC)

# Distinct English bodies (> 300 chars each, far apart in SimHash space).
BODY_RATES = (
    "The central bank left its policy rate unchanged on Tuesday and said inflation continued to ease toward its "
    "target. Officials noted that wage growth had slowed for a third straight quarter, while energy prices remained "
    "volatile because of supply disruptions in several producing regions. The statement added that the committee "
    "would review incoming data before its next meeting in December."
)
BODY_TRADE = (
    "Exports of semiconductors climbed sharply in October as shipments to data center operators increased, the "
    "customs agency reported. Port congestion eased at the main container terminals, and freight rates for routes "
    "to Europe fell for the second consecutive week. Analysts pointed to inventory rebuilding by electronics makers "
    "as a driver of the recovery in overseas demand."
)
BODY_POWER = (
    "Grid operators connected another 4 gigawatts of solar capacity during the third quarter, lifting the share of "
    "renewables in the generation mix to a record. Interconnection queues remain long because transformer deliveries "
    "are delayed, and regulators opened a consultation on faster permitting for transmission lines across several "
    "regions."
)


def page(body: str, *, title: str = "Headline", date: str | None = "2026-11-03") -> str:
    meta = f'<meta property="article:published_time" content="{date}">' if date else ""
    article = f"<article><h1>{title}</h1><p>{body}</p></article>"
    return f"<html><head><title>{title}</title>{meta}</head><body>{article}</body></html>"


def spec(source_id: str = "src_t_a", **kw: Any) -> SourceSpec:
    base_fields: dict[str, Any] = {
        "id": source_id,
        "country": "US",
        "region": "NA",
        "publisher": f"Publisher {source_id}",
        "grade": "A",
        "kind": "rss",
        "url": f"https://{source_id}.example.org/feed.xml",
        "cadence_min": 15,
        "parser": "FakeCrawler",
        "lang": "en",
    }
    base_fields.update(kw)
    return SourceSpec(**base_fields)


def raw(url: str, html: str, source_id: str = "src_t_a", published_at: datetime | None = T0) -> RawDoc:
    return RawDoc(url=url, title=None, published_at=published_at, html=html, fetched_at=T0, source_id=source_id)


SLEEPS: list[float] = []


class FakeCrawler:
    """`plan[source_id]` is what `fetch` returns (or raises); `since_seen` records the `since` each call got."""

    plan: ClassVar[dict[str, list[RawDoc] | Exception]] = {}
    script: ClassVar[dict[str, list[list[RawDoc] | Exception]]] = {}  # per-call outcomes, used before `plan`
    since_seen: ClassVar[list[tuple[str, datetime]]] = []
    respect_since: ClassVar[bool] = False  # True: drop dated documents older than `since`, like RssCrawler does
    truncate: ClassVar[set[str]] = set()  # sources that report "ran out of fetch budget" (what `collect` does)
    advance: ClassVar[dict[str, float]] = {}  # seconds of fake time one attempt of this source takes
    budgets_seen: ClassVar[list[tuple[str, float | None]]] = []  # (source id, budget seconds left at attempt start)

    def fetch(self, spec: SourceSpec, since: datetime) -> list[RawDoc]:
        FakeCrawler.since_seen.append((spec.id, since))
        budget = base.current_budget()
        FakeCrawler.budgets_seen.append((spec.id, budget.remaining() if budget else None))
        if spec.id in FakeCrawler.advance:
            base.CLOCK.sleep(FakeCrawler.advance[spec.id])
        if budget is not None and spec.id in FakeCrawler.truncate:
            budget.truncated = True
        scripted = FakeCrawler.script.get(spec.id)
        result = scripted.pop(0) if scripted else FakeCrawler.plan[spec.id]
        if isinstance(result, Exception):
            raise result
        if FakeCrawler.respect_since:
            return [r for r in result if r.published_at is None or r.published_at >= since]
        return result


@pytest.fixture(scope="module")
def pg_url() -> Iterator[str]:
    with PostgresContainer("postgres:16.15", driver="psycopg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="module")
def minio_endpoint() -> Iterator[str]:
    container = (
        DockerContainer(MINIO_IMAGE)
        .with_env("MINIO_ROOT_USER", MINIO_USER)
        .with_env("MINIO_ROOT_PASSWORD", MINIO_PASSWORD)
        .with_env("MINIO_DEFAULT_BUCKETS", BUCKET)
        .with_env("MINIO_SCHEME", "http")
        .with_exposed_ports(9000)
    )
    with container:
        endpoint = f"http://{container.get_container_host_ip()}:{container.get_exposed_port(9000)}"
        settings = make_settings("postgresql+psycopg://unused", endpoint)
        deadline = time.monotonic() + 90
        while True:
            try:
                s3_client.get_s3(settings).head_bucket(Bucket=BUCKET)
                break
            except Exception:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)
        yield endpoint


def make_settings(pg_dsn: str, s3_endpoint: str) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        pg_dsn=SecretStr(pg_dsn),
        neo4j_uri="bolt://unused",
        neo4j_user="unused",
        neo4j_password=SecretStr("unused"),
        redis_url=SecretStr("redis://unused"),
        s3_endpoint=s3_endpoint,
        s3_bucket=BUCKET,
        s3_key=SecretStr(MINIO_USER),
        s3_secret=SecretStr(MINIO_PASSWORD),
        langfuse_host="http://unused",
        langfuse_public_key="unused",
        langfuse_secret_key=SecretStr("unused"),
        anthropic_api_key=SecretStr("unused"),
        resend_api_key=SecretStr("unused"),
        from_email="unused@example.com",
    )


@pytest.fixture(scope="module")
def migrated(pg_url: str, minio_endpoint: str) -> Iterator[Engine]:
    mp = pytest.MonkeyPatch()
    env = {
        "PG_DSN": pg_url,
        "NEO4J_URI": "bolt://unused",
        "NEO4J_USER": "unused",
        "NEO4J_PASSWORD": "unused",
        "REDIS_URL": "redis://unused",
        "S3_ENDPOINT": minio_endpoint,
        "S3_BUCKET": BUCKET,
        "S3_KEY": MINIO_USER,
        "S3_SECRET": MINIO_PASSWORD,
        "LANGFUSE_HOST": "http://unused",
        "LANGFUSE_PUBLIC_KEY": "unused",
        "LANGFUSE_SECRET_KEY": "unused",
        "ANTHROPIC_API_KEY": "unused",
        "RESEND_API_KEY": "unused",
        "FROM_EMAIL": "unused@example.com",
    }
    for k, v in env.items():
        mp.setenv(k, v)
    command.upgrade(Config(str(ROOT / "alembic.ini")), "head")
    # the default engine/s3 client are process-wide singletons: point them at this module's containers
    mp.setattr(db_client, "_engine", None)
    mp.setattr(s3_client, "_client", None)
    engine = create_engine(pg_url)
    try:
        yield engine
    finally:
        engine.dispose()
        db_client.get_engine().dispose()
        mp.undo()


@pytest.fixture
def engine(migrated: Engine, monkeypatch: pytest.MonkeyPatch) -> Engine:
    """Empty ingest tables, the fake crawler registered under parser `FakeCrawler`, no real network."""
    with migrated.begin() as conn:
        conn.execute(text("TRUNCATE chunks, raw_documents, stories, sources CASCADE"))
    FakeCrawler.plan = {}
    FakeCrawler.script = {}
    FakeCrawler.since_seen = []
    FakeCrawler.respect_since = False
    FakeCrawler.truncate = set()
    FakeCrawler.advance = {}
    FakeCrawler.budgets_seen = []
    monkeypatch.setitem(CRAWLERS, "FakeCrawler", FakeCrawler)
    SLEEPS.clear()  # retry backoff must not really wait
    monkeypatch.setattr(base, "CLOCK", Clock(monotonic=lambda: 0.0, sleep=SLEEPS.append))
    return migrated


def seed(engine: Engine, *specs: SourceSpec) -> None:
    upsert_sources(list(specs), engine=engine)


def run(at: datetime) -> None:
    ingest_flow.fn(now=at)


def rows(engine: Engine, sql: str, **params: Any) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(sql), params).all())


def scalar(engine: Engine, sql: str, **params: Any) -> Any:
    return rows(engine, sql, **params)[0][0]


def counts(engine: Engine) -> tuple[int, int, int]:
    return (
        scalar(engine, "SELECT count(*) FROM raw_documents"),
        scalar(engine, "SELECT count(*) FROM chunks"),
        scalar(engine, "SELECT count(*) FROM stories"),
    )


def snapshot_exists(key: str) -> bool:
    try:
        s3_client.get_s3().head_object(Bucket=BUCKET, Key=key)
    except Exception:
        return False
    return True


# --- ingest_flow ---------------------------------------------------------------------


def test_first_run_stores_documents_chunks_and_stories(engine: Engine) -> None:
    seed(engine, spec("src_t_a"), spec("src_t_b"))
    FakeCrawler.plan = {
        "src_t_a": [
            raw("https://a.example.org/rates", page(BODY_RATES, title="Rates unchanged")),
            raw("https://a.example.org/trade", page(BODY_TRADE, title="Chip exports climb")),
        ],
        "src_t_b": [raw("https://b.example.org/power", page(BODY_POWER, title="Solar record"), "src_t_b")],
    }
    run(T0)

    assert rows(engine, "SELECT status, count(*) FROM raw_documents GROUP BY 1") == [("chunked", 3)]
    n_docs, n_chunks, n_stories = counts(engine)
    assert (n_docs, n_stories) == (3, 3)
    assert n_chunks >= 3
    # every chunk is the slice of its document text it claims to be, and every document is in a story
    for text_, start, end, chunk_text in rows(
        engine, "SELECT d.text, c.char_start, c.char_end, c.text FROM chunks c JOIN raw_documents d ON d.id = c.doc_id"
    ):
        assert text_[start:end] == chunk_text
    assert scalar(engine, "SELECT count(*) FROM raw_documents WHERE story_id IS NULL") == 0
    # snapshots are in object storage under the documented key
    for key, sha in rows(engine, "SELECT snapshot_key, snapshot_sha FROM raw_documents"):
        assert key.startswith("raw/2026/11/doc_") and key.endswith(".html")
        assert len(sha) == 64
        assert snapshot_exists(key)
    # source bookkeeping
    assert rows(engine, "SELECT id, last_ok_at, fail_count FROM sources ORDER BY id") == [
        ("src_t_a", T0, 0),
        ("src_t_b", T0, 0),
    ]


def test_second_run_is_idempotent(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {
        "src_t_a": [
            raw("https://a.example.org/rates", page(BODY_RATES)),
            raw("https://a.example.org/short", page("Too short.")),
        ]
    }
    run(T0)
    before = counts(engine)
    n_skipped = scalar(engine, "SELECT count(*) FROM raw_documents WHERE status = 'skipped'")
    assert n_skipped == 1

    run(T0 + timedelta(hours=1))  # due again; the crawler returns the very same documents

    assert counts(engine) == before
    assert scalar(engine, "SELECT count(*) FROM raw_documents") == 2
    assert len(FakeCrawler.since_seen) == 2  # it did run


def test_since_is_last_ok_at_or_24h_before_now(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {"src_t_a": []}
    run(T0)
    run(T0 + timedelta(minutes=20))
    assert FakeCrawler.since_seen == [("src_t_a", T0 - timedelta(hours=24)), ("src_t_a", T0)]


def test_transient_error_is_retried_with_backoff_and_a_later_success_is_not_a_failure(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    doc = raw("https://a.example.org/rates", page(BODY_RATES))
    FakeCrawler.script = {"src_t_a": [TransientError("http 503"), TransientError("http 503"), [doc]]}
    run(T0)
    assert len(FakeCrawler.since_seen) == 3
    assert SLEEPS == [2.0, 4.0]
    assert rows(engine, "SELECT fail_count, last_error, last_ok_at FROM sources") == [(0, None, T0)]
    assert rows(engine, "SELECT status FROM raw_documents") == [("chunked",)]


def test_three_transient_failures_count_as_one_failure(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {"src_t_a": TransientError("http 503")}
    run(T0)
    assert len(FakeCrawler.since_seen) == 3
    assert SLEEPS == [2.0, 4.0]  # no wait after the last attempt
    assert rows(engine, "SELECT fail_count, enabled, last_ok_at FROM sources") == [(1, True, None)]


def test_permanent_error_is_not_retried(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {"src_t_a": PermanentError("feed unparsable")}
    run(T0)
    assert len(FakeCrawler.since_seen) == 1
    assert SLEEPS == []
    assert scalar(engine, "SELECT fail_count FROM sources") == 1


def test_source_not_yet_due_is_not_crawled(engine: Engine) -> None:
    seed(engine, spec("src_t_a", cadence_min=60))
    FakeCrawler.plan = {"src_t_a": []}
    run(T0)
    run(T0 + timedelta(minutes=30))  # cadence 60 not reached
    assert len(FakeCrawler.since_seen) == 1
    run(T0 + timedelta(minutes=60))
    assert len(FakeCrawler.since_seen) == 2


def test_short_document_is_stored_as_skipped_with_its_snapshot(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {"src_t_a": [raw("https://a.example.org/stub", page("Short."))]}
    run(T0)

    (row,) = rows(
        engine,
        "SELECT status, text, text_sha, simhash, story_id, snapshot_key, snapshot_sha, doc_type::text, lang, url_hash "
        "FROM raw_documents",
    )
    status, text_, text_sha, simhash, story_id, key, sha, doc_type, lang, uh = row
    assert status == "skipped"
    assert (text_, simhash, story_id) == ("", 0, None)
    assert text_sha == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # sha256("")
    assert key.startswith("raw/2026/11/doc_") and len(sha) == 64
    assert snapshot_exists(key)
    assert (doc_type, lang, uh) == ("RELEASE", "en", url_hash("https://a.example.org/stub"))
    assert counts(engine) == (1, 0, 0)  # no chunks, no story


def test_skipped_snapshot_is_written_exactly_once(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {"src_t_a": [raw("https://a.example.org/stub", page("Short."))]}
    puts: list[str] = []
    real_put = s3_client.put_immutable

    def counting_put(key: str, body: bytes, **kw: Any) -> bool:
        puts.append(key)
        return real_put(key, body, **kw)

    monkeypatch.setattr("domino_ingest.normalizer.put_immutable", counting_put)
    run(T0)
    run(T0 + timedelta(hours=1))
    assert len(puts) == 1  # insert_skipped did not store it again, and the rerun never normalized it


def test_five_consecutive_failures_disable_the_source(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {"src_t_a": TransientError("http 503")}
    for i in range(4):
        run(T0 + timedelta(minutes=15 * i))
        assert rows(engine, "SELECT fail_count, enabled FROM sources") == [(i + 1, True)]
    run(T0 + timedelta(minutes=60))
    fail_count, enabled, last_error, last_ok_at = rows(
        engine, "SELECT fail_count, enabled, last_error, last_ok_at FROM sources"
    )[0]
    assert (fail_count, enabled, last_ok_at) == (5, False, None)
    assert "http 503" in last_error

    calls = len(FakeCrawler.since_seen)
    run(T0 + timedelta(hours=3))  # a disabled source is never crawled again
    assert len(FakeCrawler.since_seen) == calls


def test_success_resets_the_failure_count(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {"src_t_a": TransientError("http 503")}
    run(T0)
    run(T0 + timedelta(minutes=15))
    assert scalar(engine, "SELECT fail_count FROM sources") == 2
    FakeCrawler.plan = {"src_t_a": []}
    run(T0 + timedelta(minutes=30))
    assert rows(engine, "SELECT fail_count, last_error, enabled FROM sources") == [(0, None, True)]


def test_one_sources_permanent_error_does_not_block_the_others(engine: Engine) -> None:
    seed(engine, spec("src_t_a"), spec("src_t_b"), spec("src_t_c"))
    FakeCrawler.plan = {
        "src_t_a": PermanentError("feed unparsable"),
        "src_t_b": [raw("https://b.example.org/power", page(BODY_POWER), "src_t_b")],
        "src_t_c": TransientError("http 503"),
    }
    run(T0)
    assert rows(engine, "SELECT id, fail_count, last_ok_at FROM sources ORDER BY id") == [
        ("src_t_a", 1, None),
        ("src_t_b", 0, T0),
        ("src_t_c", 1, None),
    ]
    assert rows(engine, "SELECT source_id, status FROM raw_documents") == [("src_t_b", "chunked")]


def test_unexpected_normalize_error_skips_only_that_document(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    failed_at = T0 - timedelta(hours=2)
    naive = raw("https://a.example.org/naive", page(BODY_RATES), published_at=failed_at)
    naive = naive.model_copy(update={"fetched_at": datetime(2026, 11, 4)})  # naive datetime → PermanentError
    FakeCrawler.plan = {"src_t_a": [naive, raw("https://a.example.org/trade", page(BODY_TRADE))]}
    run(T0)
    assert rows(engine, "SELECT url, status FROM raw_documents") == [("https://a.example.org/trade", "chunked")]
    # the source itself was fine, but `last_ok_at` stops at the failed document so the next run fetches it again (H1)
    assert rows(engine, "SELECT fail_count, last_ok_at FROM sources") == [(0, failed_at)]


def test_document_failure_holds_last_ok_at(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review H1: a storage failure (S3 403) must not move `last_ok_at` past the document, or it is lost for good."""
    seed(engine, spec("src_t_a"))
    published = T0 - timedelta(minutes=10)
    FakeCrawler.respect_since = True
    FakeCrawler.plan = {"src_t_a": [raw("https://a.example.org/rates", page(BODY_RATES), published_at=published)]}

    with monkeypatch.context() as m:
        m.setattr("domino_ingest.normalizer.put_immutable", _raise_permanent)
        run(T0)
    assert rows(engine, "SELECT last_ok_at, fail_count FROM sources") == [(published, 0)]
    assert counts(engine) == (0, 0, 0)

    run(T0 + timedelta(minutes=15))  # S3 is back: `since` is the failed entry's date, so the crawler returns it again
    assert FakeCrawler.since_seen[-1] == ("src_t_a", published)
    assert rows(engine, "SELECT url, status FROM raw_documents") == [("https://a.example.org/rates", "chunked")]
    assert rows(engine, "SELECT last_ok_at, fail_count FROM sources") == [(T0 + timedelta(minutes=15), 0)]


def _raise_permanent(*_a: Any, **_k: Any) -> bool:
    raise PermanentError("s3 403")


def test_undated_document_failure_does_not_hold_last_ok_at_back(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed document without `published_at` cannot hold `last_ok_at` back (undated entries are re-fetched)."""
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {"src_t_a": [raw("https://a.example.org/undated", page(BODY_RATES), published_at=None)]}
    monkeypatch.setattr("domino_ingest.normalizer.put_immutable", _raise_permanent)
    run(T0)
    assert rows(engine, "SELECT last_ok_at, fail_count FROM sources") == [(T0, 0)]


def test_unexpected_exception_marks_source_failed(engine: Engine) -> None:
    """Review L3: a library exception (not a DominoError) fails only that source."""
    seed(engine, spec("src_t_a"), spec("src_t_b"))
    FakeCrawler.plan = {
        "src_t_a": OverflowError("timestamp out of range"),
        "src_t_b": [raw("https://b.example.org/power", page(BODY_POWER), "src_t_b")],
    }
    run(T0)
    (a_fail, a_error, a_ok), (b_fail, b_error, b_ok) = rows(
        engine, "SELECT fail_count, last_error, last_ok_at FROM sources ORDER BY id"
    )
    assert a_fail == 1 and a_error.startswith("OverflowError") and a_ok is None
    assert (b_fail, b_error, b_ok) == (0, None, T0)
    assert rows(engine, "SELECT source_id, status FROM raw_documents") == [("src_t_b", "chunked")]
    assert len(FakeCrawler.since_seen) == 2  # not retried, and the second source still ran


def test_unexpected_exception_while_storing_fails_only_that_document(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-DominoError inside the document loop is a failed document (decision W2-15), not a dead flow."""
    seed(engine, spec("src_t_a"))
    published = T0 - timedelta(minutes=5)
    FakeCrawler.plan = {
        "src_t_a": [
            raw("https://a.example.org/rates", page(BODY_RATES), published_at=published),
            raw("https://a.example.org/trade", page(BODY_TRADE)),
        ]
    }
    real = store.insert_chunk
    calls = {"n": 0}

    def flaky(chunk: Chunk, **kw: Any) -> bool:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("boom")
        return real(chunk, **kw)

    monkeypatch.setattr(store, "insert_chunk", flaky)
    run(T0)
    assert rows(engine, "SELECT url, status FROM raw_documents") == [("https://a.example.org/trade", "chunked")]
    assert rows(engine, "SELECT last_ok_at, fail_count FROM sources") == [(published, 0)]


def test_chunk_uses_settings(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review L1: CHUNK_TOKENS reaches the chunker."""
    seed(engine, spec("src_t_a"))
    body = "".join(f"<p>{b}</p>" for b in (BODY_RATES, BODY_TRADE, BODY_POWER))
    html = f"<html><head><title>T</title></head><body><article><h1>Headline</h1>{body}</article></body></html>"
    FakeCrawler.plan = {"src_t_a": [raw("https://a.example.org/long", html)]}

    monkeypatch.setenv("CHUNK_TOKENS", "128")
    get_settings.cache_clear()
    run(T0)

    sizes = [r[0] for r in rows(engine, "SELECT n_tokens FROM chunks")]
    assert len(sizes) >= 2  # the default 512 would keep this document in one chunk
    assert max(sizes) <= 128


def test_wire_source_sets_wire_origin(engine: Engine) -> None:
    """Review H3: a wire source's own documents carry `sources.wire`, so original and reprint count one publisher."""
    seed(engine, spec("src_t_a", wire="reuters", publisher="Reuters"), spec("src_t_b"))
    FakeCrawler.plan = {
        "src_t_a": [raw("https://a.example.org/rates", page(BODY_RATES, title="Rates unchanged"))],  # no byline
        "src_t_b": [raw("https://b.example.org/copy", page(BODY_RATES, title="Rates unchanged"), "src_t_b")],
    }
    run(T0)
    origins = dict(rows(engine, "SELECT source_id, wire_origin FROM raw_documents"))
    assert origins == {"src_t_a": "reuters", "src_t_b": "reuters"}
    assert rows(engine, "SELECT n_docs, n_publishers FROM stories") == [(2, 1)]


def test_skipped_has_reason(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {
        "src_t_a": [
            raw("https://a.example.org/stub", page("Short.")),
            raw("https://a.example.org/rates", page(BODY_RATES)),
        ]
    }
    run(T0)
    assert rows(engine, "SELECT url, status, skip_reason FROM raw_documents ORDER BY url") == [
        ("https://a.example.org/rates", "chunked", None),
        ("https://a.example.org/stub", "skipped", "too_short"),
    ]


def test_reprint_in_the_same_story_counts_one_publisher(engine: Engine) -> None:
    seed(engine, spec("src_t_a"), spec("src_t_b"))
    FakeCrawler.plan = {
        "src_t_a": [raw("https://a.example.org/rates", page(BODY_RATES, title="Rates unchanged"))],
        "src_t_b": [raw("https://b.example.org/copy", page(BODY_RATES, title="Rates unchanged"), "src_t_b")],
    }
    run(T0)
    assert scalar(engine, "SELECT count(DISTINCT story_id) FROM raw_documents") == 1
    (n_docs, n_publishers, languages, countries) = rows(
        engine, "SELECT n_docs, n_publishers, languages, countries FROM stories"
    )[0]
    assert (n_docs, n_publishers, languages, countries) == (2, 1, ["en"], ["US"])
    origin = scalar(engine, "SELECT wire_origin FROM raw_documents WHERE source_id = 'src_t_b'")
    assert origin == "Publisher src_t_a"
    assert scalar(engine, "SELECT wire_origin FROM raw_documents WHERE source_id = 'src_t_a'") is None


def test_undated_rss_entries_are_not_downloaded_again(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    """The real RssCrawler on fake HTTP: a feed entry without a date is fetched on run 1 only."""
    feed = (FIXTURES / "feed.xml").read_text(encoding="utf-8")
    article = (
        (FIXTURES / "article.html").read_text(encoding="utf-8").replace("</article>", f"<p>{BODY_RATES}</p></article>")
    )
    calls: list[str] = []

    def fake_get(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> tuple[int, str, dict[str, str]]:
        calls.append(url)
        if url.endswith("/robots.txt"):
            raise PermanentError("http 404")
        if url == "https://feeds.example.org/feed.xml":
            return 200, feed, {}
        if url.startswith("https://www.example.org/news/"):
            return 200, article, {}
        raise PermanentError(f"http 404: {url}")

    base.reset_state()
    monkeypatch.setattr(http, "get", fake_get)
    monkeypatch.setattr(base, "CLOCK", Clock(monotonic=lambda: 0.0, sleep=lambda _s: None))
    seed(engine, spec("src_t_rss", url="https://feeds.example.org/feed.xml", parser="RssCrawler"))

    run(T0 - timedelta(days=365))  # window reaches back far enough to take every dated entry too
    articles = [c for c in calls if c.startswith("https://www.example.org/news/")]
    assert "https://www.example.org/news/d-undated" in articles
    first = len(articles)
    assert scalar(engine, "SELECT count(*) FROM raw_documents") == first

    calls.clear()
    run(T0 + timedelta(days=1))
    assert [c for c in calls if c.startswith("https://www.example.org/news/")] == []  # nothing downloaded twice
    assert scalar(engine, "SELECT count(*) FROM raw_documents") == first
    base.reset_state()


# --- fetch budgets (week-2 T3, decision W2-1) ---------------------------------------------


class FlowTimeout(BaseException):
    """Prefect killing the flow at its 600 s limit (a BaseException: `except Exception` does not catch it)."""


class FakeWeb:
    """Fake `clients.http.get` and a fake clock; sleeping advances time. Hosts in `slow` have `Crawl-delay: 30`."""

    flow_limit_s = 600.0

    def __init__(self) -> None:
        self.t = 0.0
        self.run_start = 0.0
        self.enforce_limit = False
        self.routes: dict[str, str] = {}
        self.slow: set[str] = set()
        self.calls: list[str] = []

    def monotonic(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds
        if self.enforce_limit and self.t - self.run_start > self.flow_limit_s:
            raise FlowTimeout

    def get(
        self, url: str, *, timeout: float, headers: dict[str, str] | None = None
    ) -> tuple[int, str, dict[str, str]]:
        self.calls.append(url)
        if url.endswith("/robots.txt"):
            host = url.removesuffix("/robots.txt")
            if host in self.slow:
                return 200, "User-agent: *\nCrawl-delay: 30\n", {}
            raise PermanentError("http 404")
        if url in self.routes:
            return 200, self.routes[url], {}
        raise PermanentError(f"http 404: {url}")

    def articles(self, source_id: str) -> list[str]:
        return [c for c in self.calls if c.startswith(f"https://{source_id}.example.org/news/")]

    def add_slow_site(self, source_id: str, n: int) -> None:
        """A feed of `n` entries (all within the 24 h lookback) on a Crawl-delay-30 host."""
        host = f"https://{source_id}.example.org"
        self.slow.add(host)
        published = (T0 - timedelta(hours=1)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        items = "".join(
            f"<item><title>Report {i}</title><link>{host}/news/{i}</link><pubDate>{published}</pubDate></item>"
            for i in range(n)
        )
        self.routes[f"{host}/feed.xml"] = (
            f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'
        )
        for i in range(n):
            self.routes[f"{host}/news/{i}"] = page(f"{BODY_RATES} Report number {i}.", title=f"Report {i}")

    def run(self, at: datetime) -> float:
        """One ingest run at `at`; returns the fake seconds it took. The clock then moves 15 minutes on."""
        self.run_start = self.t
        ingest_flow.fn(now=at)
        elapsed = self.t - self.run_start
        self.t += 900
        return elapsed


@pytest.fixture
def web(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    fake = FakeWeb()
    base.reset_state()
    monkeypatch.setattr(base, "CLOCK", Clock(monotonic=fake.monotonic, sleep=fake.sleep))
    monkeypatch.setattr(http, "get", fake.get)
    return fake


def slow_spec(source_id: str) -> SourceSpec:
    return spec(source_id, parser="RssCrawler")


def source_row(engine: Engine, source_id: str) -> tuple[datetime | None, int]:
    last_ok_at, fail_count = rows(engine, "SELECT last_ok_at, fail_count FROM sources WHERE id = :i", i=source_id)[0]
    return last_ok_at, fail_count


def n_docs(engine: Engine, source_id: str) -> int:
    return int(scalar(engine, "SELECT count(*) FROM raw_documents WHERE source_id = :i", i=source_id))


def test_truncated_source_does_not_advance(engine: Engine, web: FakeWeb) -> None:
    """Review H2: a source that ran out of fetch budget keeps `last_ok_at`, stores what it got, and converges."""
    since = T0 - timedelta(hours=24)
    seed(engine, slow_spec("src_t_a"))
    web.add_slow_site("src_t_a", 10)

    stored: list[int] = []
    last_ok: list[datetime | None] = []
    for k in range(5):
        web.run(T0 + timedelta(minutes=15 * k))
        stored.append(n_docs(engine, "src_t_a"))
        last_ok.append(source_row(engine, "src_t_a")[0])
        if last_ok[-1] != since:
            break

    assert stored[0] > 0  # what was downloaded before the budget ran out is stored, not thrown away
    assert last_ok[0] == since  # ... but the source did not advance: the rest of the feed is still owed
    assert stored == sorted(set(stored))  # every truncated run made progress
    assert stored[-1] == 10  # converged: the run that got everything is the one that moved `last_ok_at`
    assert last_ok[-1] == T0 + timedelta(minutes=15 * (len(stored) - 1))
    assert len(stored) >= 2
    assert source_row(engine, "src_t_a")[1] == 0
    articles = web.articles("src_t_a")
    assert len(articles) == len(set(articles)) == 10  # later runs continue; nothing is downloaded twice (skip_known)


def test_slow_source_does_not_starve_others(engine: Engine, web: FakeWeb) -> None:
    """Review H2: a 40-entry Crawl-delay-30 source first in id order must not keep the other sources from running."""
    seed(engine, slow_spec("src_a_slow"), spec("src_b"), spec("src_c"), spec("src_d"))
    web.add_slow_site("src_a_slow", 40)
    FakeCrawler.plan = {
        "src_b": [raw("https://b.example.org/rates", page(BODY_RATES), "src_b")],
        "src_c": [raw("https://c.example.org/trade", page(BODY_TRADE), "src_c")],
        "src_d": [raw("https://d.example.org/power", page(BODY_POWER), "src_d")],
    }
    web.enforce_limit = True  # the old code needs 40 x 30 s here and dies at the flow's 600 s limit

    elapsed = web.run(T0)

    assert elapsed <= ingest_module.FLOW_FETCH_BUDGET_S
    for other in ("src_b", "src_c", "src_d"):
        assert source_row(engine, other) == (T0, 0)  # `mark_ok`
        assert n_docs(engine, other) == 1
    last_ok_at, fail_count = source_row(engine, "src_a_slow")
    assert (last_ok_at, fail_count) == (T0 - timedelta(hours=24), 0)
    assert 0 < n_docs(engine, "src_a_slow") < 40


def test_flow_budget_leaves_the_rest_untouched_and_they_go_first_next_run(engine: Engine, web: FakeWeb) -> None:
    """Four slow sources use 120 + 120 + 120 + 90 s of the 450 s flow budget; the source after them is not touched."""
    slow_ids = ["src_s1", "src_s2", "src_s3", "src_s4"]
    seed(engine, *[slow_spec(i) for i in slow_ids], spec("src_z_fast"))
    for i in slow_ids:
        web.add_slow_site(i, 40)
    FakeCrawler.plan = {"src_z_fast": [raw("https://z.example.org/rates", page(BODY_RATES), "src_z_fast")]}

    elapsed = web.run(T0)

    assert elapsed == ingest_module.FLOW_FETCH_BUDGET_S == 450  # 120 + 120 + 120 + min(120, 450 - 360)
    assert [n_docs(engine, i) for i in slow_ids] == [4, 4, 4, 3]
    assert FakeCrawler.since_seen == []  # src_z_fast was not even crawled: < 10 s of budget left
    assert source_row(engine, "src_z_fast") == (None, 0)  # still due, state unchanged

    web.run(T0 + timedelta(minutes=15))  # `last_ok_at IS NULL` sorts first: the untouched source is first in line
    assert [sid for sid, _ in FakeCrawler.since_seen] == ["src_z_fast"]
    assert source_row(engine, "src_z_fast")[0] == T0 + timedelta(minutes=15)
    assert [n_docs(engine, i) for i in slow_ids[:2]] == [8, 8]  # and the slow ones keep converging


def test_source_budget_is_the_smaller_of_120_and_the_flow_time_left(engine: Engine, web: FakeWeb) -> None:
    seed(engine, spec("src_t_a"), spec("src_t_b"), spec("src_t_c"))
    FakeCrawler.plan = {"src_t_a": [], "src_t_b": [], "src_t_c": []}
    FakeCrawler.advance = {"src_t_a": 400.0, "src_t_b": 45.0}  # a overruns its 120 s; b uses 45 of what is left
    web.run(T0)
    budgets = dict(FakeCrawler.budgets_seen)
    assert budgets == {"src_t_a": pytest.approx(120.0), "src_t_b": pytest.approx(50.0)}
    assert [sid for sid, _ in FakeCrawler.budgets_seen] == ["src_t_a", "src_t_b"]  # 5 s left: src_t_c untouched
    assert source_row(engine, "src_t_c") == (None, 0)
    assert source_row(engine, "src_t_a")[0] == T0 and source_row(engine, "src_t_b")[0] == T0


def test_retries_share_the_source_budget(engine: Engine, web: FakeWeb) -> None:
    seed(engine, spec("src_t_a"))
    doc = raw("https://a.example.org/rates", page(BODY_RATES))
    FakeCrawler.script = {"src_t_a": [TransientError("http 503"), TransientError("http 503"), [doc]]}
    FakeCrawler.advance = {"src_t_a": 30.0}
    web.run(T0)
    left = [b for _, b in FakeCrawler.budgets_seen]
    # one 120 s budget for all three attempts: 120, then 120 - 30 - 2 (backoff), then 88 - 30 - 4
    assert left == [pytest.approx(120.0), pytest.approx(88.0), pytest.approx(54.0)]
    assert source_row(engine, "src_t_a") == (T0, 0)
    assert n_docs(engine, "src_t_a") == 1


def test_no_retry_once_the_source_budget_is_spent(engine: Engine, web: FakeWeb) -> None:
    seed(engine, spec("src_t_a"))
    FakeCrawler.script = {"src_t_a": [TransientError("http 503"), [raw("https://a.example.org/x", page(BODY_RATES))]]}
    FakeCrawler.advance = {"src_t_a": 130.0}  # the first attempt alone outlasts the 120 s budget
    web.run(T0)
    assert len(FakeCrawler.since_seen) == 1  # a retry would be truncated at once and could only fail the same way
    assert source_row(engine, "src_t_a") == (None, 1)  # reported as the failure it is


def test_truncated_source_without_a_last_ok_at_pins_it_to_the_24h_lookback(engine: Engine, web: FakeWeb) -> None:
    """The first-ever run of a source: `last_ok_at` becomes `since` (now - 24 h), so the next run does not slide."""
    seed(engine, spec("src_t_a"))
    FakeCrawler.plan = {"src_t_a": [raw("https://a.example.org/rates", page(BODY_RATES))]}
    FakeCrawler.truncate = {"src_t_a"}
    web.run(T0)
    assert source_row(engine, "src_t_a") == (T0 - timedelta(hours=24), 0)
    assert n_docs(engine, "src_t_a") == 1


@pytest.mark.parametrize(
    ("failed_published", "expected"),
    [
        (timedelta(minutes=-30), timedelta(hours=-2)),  # failed document newer than `since`: the truncation rule wins
        (timedelta(hours=-3), timedelta(hours=-3)),  # failed document older than `since`: the document rule wins
    ],
)
def test_truncation_and_failed_document_keep_the_older_last_ok_at(
    engine: Engine,
    web: FakeWeb,
    monkeypatch: pytest.MonkeyPatch,
    failed_published: timedelta,
    expected: timedelta,
) -> None:
    seed(engine, spec("src_t_a"))
    with connection(engine=engine) as conn:
        conn.execute(text("UPDATE sources SET last_ok_at = :t"), {"t": T0 - timedelta(hours=2)})
    FakeCrawler.plan = {
        "src_t_a": [raw("https://a.example.org/rates", page(BODY_RATES), published_at=T0 + failed_published)]
    }
    FakeCrawler.truncate = {"src_t_a"}
    monkeypatch.setattr("domino_ingest.normalizer.put_immutable", _raise_permanent)
    web.run(T0)
    assert source_row(engine, "src_t_a") == (T0 + expected, 0)


def test_due_sources_order(engine: Engine) -> None:
    """Review H2: the source that has waited longest goes first; never-succeeded sources before all; ties by id."""
    seed(engine, spec("src_t_a"), spec("src_t_b"), spec("src_t_c"), spec("src_t_d"), spec("src_t_e"), spec("src_t_f"))
    ages = {"src_t_a": 1, "src_t_c": 5, "src_t_d": 2, "src_t_e": 5}  # hours; src_t_b and src_t_f never succeeded
    with connection(engine=engine) as conn:
        for sid, hours in ages.items():
            conn.execute(
                text("UPDATE sources SET last_ok_at = :t WHERE id = :i"), {"t": T0 - timedelta(hours=hours), "i": sid}
            )
    assert [s.id for s, _ in store.due_sources(T0)] == [
        "src_t_b",
        "src_t_f",  # NULL first, id order among them
        "src_t_c",
        "src_t_e",  # oldest first (5 h), id order on a tie
        "src_t_d",
        "src_t_a",
    ]


# --- store ---------------------------------------------------------------------------


def document(doc_id: str, source_id: str = "src_t_a", **kw: Any) -> Document:
    fields: dict[str, Any] = {
        "id": doc_id,
        "source_id": source_id,
        "url": f"https://{source_id}.example.org/{doc_id}",
        "url_hash": url_hash(f"https://{source_id}.example.org/{doc_id}"),
        "title": "A title",
        "published_at": T0,
        "lang": "en",
        "doc_type": "NEWS",
        "text": BODY_RATES,
        "text_sha": "0" * 64,
        "simhash": 5,
        "snapshot_key": f"raw/2026/11/{doc_id}.html",
        "snapshot_sha": "1" * 64,
        "story_id": "sty_T1",
    }
    fields.update(kw)
    return Document(**fields)


def test_simhash_is_stored_signed_and_read_back_unsigned(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    big = (1 << 63) + 12345  # does not fit a signed bigint as is
    doc = document("doc_big", simhash=big)
    assert store.insert_document(doc) is True
    store.upsert_story(doc, now=T0)
    assert scalar(engine, "SELECT simhash FROM raw_documents WHERE id = 'doc_big'") == to_signed(big) < 0
    assert scalar(engine, "SELECT simhash FROM stories WHERE id = 'sty_T1'") == to_signed(big)
    (head,) = store.recent_story_heads(timedelta(hours=72), now=T0)
    assert (head.id, head.simhash) == ("sty_T1", big)
    (back,) = store.story_docs("sty_T1")
    assert back == doc


def test_insert_document_is_idempotent_on_url_hash(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    doc = document("doc_one")
    assert store.exists(doc.url_hash) is False
    assert store.insert_document(doc) is True
    assert store.exists(doc.url_hash) is True
    assert store.insert_document(doc.model_copy(update={"id": "doc_two"})) is False  # same url_hash
    assert scalar(engine, "SELECT count(*) FROM raw_documents") == 1
    assert store.known_url_hashes([doc.url_hash, "f" * 64]) == {doc.url_hash}


def test_insert_chunk_is_idempotent_per_doc_and_idx(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    doc = document("doc_one")
    store.insert_document(doc)
    chunk = Chunk(id="chk_1", doc_id="doc_one", idx=0, char_start=0, char_end=5, text="hello", n_tokens=1)
    store.insert_chunk(chunk)
    store.insert_chunk(chunk.model_copy(update={"id": "chk_2"}))
    assert rows(engine, "SELECT id FROM chunks") == [("chk_1",)]
    store.set_status("doc_one", "chunked")
    assert scalar(engine, "SELECT status FROM raw_documents WHERE id = 'doc_one'") == "chunked"


def test_story_title_is_the_best_grade_documents_title_else_its_first_120_chars(engine: Engine) -> None:
    seed(engine, spec("src_t_a", grade="B"), spec("src_t_b", grade="A"), spec("src_t_c", grade="C"))
    first = document("doc_a", "src_t_a", title="Grade B headline")
    store.insert_document(first)
    store.upsert_story(first, now=T0)
    assert scalar(engine, "SELECT title FROM stories") == "Grade B headline"

    best = document("doc_b", "src_t_b", title=None, text="x" * 200)  # grade A but untitled → text[:120]
    store.insert_document(best)
    store.upsert_story(best, now=T0 + timedelta(minutes=5))
    assert scalar(engine, "SELECT title FROM stories") == "x" * 120

    worst = document("doc_c", "src_t_c", title="Grade C headline")
    store.insert_document(worst)
    store.upsert_story(worst, now=T0 + timedelta(minutes=10))
    assert rows(engine, "SELECT title, n_docs, n_publishers, first_seen, last_seen FROM stories") == [
        ("x" * 120, 3, 3, T0, T0 + timedelta(minutes=10))
    ]


def test_recent_story_heads_only_returns_the_window(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    old = document("doc_old", story_id="sty_old")
    new = document("doc_new", story_id="sty_new")
    for d, seen in ((old, T0 - timedelta(hours=80)), (new, T0 - timedelta(hours=10))):
        store.insert_document(d)
        store.upsert_story(d, now=seen)
    assert [h.id for h in store.recent_story_heads(timedelta(hours=72), now=T0)] == ["sty_new"]


def test_due_sources_returns_state_and_skips_disabled_and_not_due(engine: Engine) -> None:
    seed(
        engine,
        spec("src_t_a", cadence_min=15),
        spec("src_t_b", cadence_min=60),
        spec("src_t_c", enabled=False),
        spec("src_t_d"),
    )
    with connection(engine=engine) as conn:
        conn.execute(
            text("UPDATE sources SET last_ok_at = :t, fail_count = 2 WHERE id = 'src_t_a'"),
            {"t": T0 - timedelta(hours=1)},
        )
        conn.execute(text("UPDATE sources SET last_ok_at = :t WHERE id = 'src_t_b'"), {"t": T0 - timedelta(minutes=30)})
    due = store.due_sources(T0)
    assert [(s.id, st.last_ok_at, st.fail_count) for s, st in due] == [
        ("src_t_d", None, 0),  # never succeeded: first (oldest-first order, W2-1)
        ("src_t_a", T0 - timedelta(hours=1), 2),
    ]
    assert due[1][0] == spec("src_t_a", cadence_min=15)


def test_due_sources_tolerates_one_minute_of_cadence_drift(engine: Engine) -> None:
    seed(engine, spec("src_t_a", cadence_min=30))
    for ago, is_due in (
        (timedelta(minutes=29, seconds=30), True),
        (timedelta(minutes=29), True),
        (timedelta(minutes=28), False),
    ):
        with connection(engine=engine) as conn:
            conn.execute(text("UPDATE sources SET last_ok_at = :t"), {"t": T0 - ago})
        assert [s.id for s, _ in store.due_sources(T0)] == (["src_t_a"] if is_due else []), ago


def test_insert_skipped_uses_the_snapshot_the_error_carries(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    r = raw("https://a.example.org/stub", "<html></html>")
    store.insert_skipped(r, TooShortError("doc_stub", "raw/2026/11/doc_stub.html", "2" * 64), spec("src_t_a"))
    assert rows(engine, "SELECT id, status, snapshot_key, snapshot_sha FROM raw_documents") == [
        ("doc_stub", "skipped", "raw/2026/11/doc_stub.html", "2" * 64)
    ]
    store.insert_skipped(r, TooShortError("doc_other", "raw/2026/11/doc_other.html", "3" * 64), spec("src_t_a"))
    assert scalar(engine, "SELECT count(*) FROM raw_documents") == 1  # same url_hash: no second row


def test_insert_skipped_records_the_reason(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    store.insert_skipped(
        raw("https://a.example.org/x", "<html></html>"),
        LangUndetectedError("doc_x", "raw/2026/11/doc_x.html", "4" * 64),
        spec("src_t_a"),
    )
    assert rows(engine, "SELECT status, skip_reason FROM raw_documents") == [("skipped", "lang_undetected")]
