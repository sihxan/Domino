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
from domino_core.settings import Settings
from domino_ingest import store
from domino_ingest.crawlers import CRAWLERS, base
from domino_ingest.crawlers.base import Clock
from domino_ingest.normalizer import TooShortError, url_hash
from domino_ingest.simhash import to_signed
from domino_ingest.sources import upsert_sources
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


class FakeCrawler:
    """`plan[source_id]` is what `fetch` returns (or raises); `since_seen` records the `since` each call got."""

    plan: ClassVar[dict[str, list[RawDoc] | Exception]] = {}
    since_seen: ClassVar[list[tuple[str, datetime]]] = []

    def fetch(self, spec: SourceSpec, since: datetime) -> list[RawDoc]:
        FakeCrawler.since_seen.append((spec.id, since))
        result = FakeCrawler.plan[spec.id]
        if isinstance(result, Exception):
            raise result
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
        pg_dsn=pg_dsn,
        neo4j_uri="bolt://unused",
        neo4j_user="unused",
        neo4j_password=SecretStr("unused"),
        redis_url="redis://unused",
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
    FakeCrawler.since_seen = []
    monkeypatch.setitem(CRAWLERS, "FakeCrawler", FakeCrawler)
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
    naive = raw("https://a.example.org/naive", page(BODY_RATES))
    naive = naive.model_copy(update={"fetched_at": datetime(2026, 11, 4)})  # naive datetime → PermanentError
    FakeCrawler.plan = {"src_t_a": [naive, raw("https://a.example.org/trade", page(BODY_TRADE))]}
    run(T0)
    assert rows(engine, "SELECT url, status FROM raw_documents") == [("https://a.example.org/trade", "chunked")]
    assert scalar(engine, "SELECT fail_count FROM sources") == 0  # the source itself was fine


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
        ("src_t_a", T0 - timedelta(hours=1), 2),
        ("src_t_d", None, 0),
    ]
    assert due[0][0] == spec("src_t_a", cadence_min=15)


def test_insert_skipped_uses_the_snapshot_the_error_carries(engine: Engine) -> None:
    seed(engine, spec("src_t_a"))
    r = raw("https://a.example.org/stub", "<html></html>")
    store.insert_skipped(r, TooShortError("doc_stub", "raw/2026/11/doc_stub.html", "2" * 64), spec("src_t_a"))
    assert rows(engine, "SELECT id, status, snapshot_key, snapshot_sha FROM raw_documents") == [
        ("doc_stub", "skipped", "raw/2026/11/doc_stub.html", "2" * 64)
    ]
    store.insert_skipped(r, TooShortError("doc_other", "raw/2026/11/doc_other.html", "3" * 64), spec("src_t_a"))
    assert scalar(engine, "SELECT count(*) FROM raw_documents") == 1  # same url_hash: no second row
