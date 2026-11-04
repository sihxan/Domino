"""Postgres store for ingest: due sources, documents, stories, chunks, source bookkeeping (idempotent SQL)."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import Connection, text

from domino_core.clients.db import connection
from domino_core.log import get_logger
from domino_core.models import Chunk, Document, RawDoc, SourceSpec
from domino_core.timeutil import now_utc
from domino_ingest.normalizer import SkippedDocError, doc_type_for, url_hash
from domino_ingest.simhash import to_signed, to_unsigned
from domino_ingest.story import STORY_WINDOW, StoryHead

CADENCE_TOLERANCE = timedelta(minutes=1)  # a source is due once `now - last_ok_at >= cadence_min - 1 minute`
MAX_FAILS = 5  # consecutive failed crawls before a source is disabled (tech §5 ingest.fetch)

log = get_logger(__name__)


@dataclass(frozen=True)
class SourceState:
    """Operational state of a source row (not part of `SourceSpec`): when it last succeeded, current failure streak."""

    last_ok_at: datetime | None
    fail_count: int


@contextmanager
def _conn(conn: Connection | None) -> Iterator[Connection]:
    """The caller's connection (one transaction across several calls), or a fresh transactional one."""
    if conn is not None:
        yield conn
    else:
        with connection() as new:
            yield new


_DUE = text(
    """
    SELECT id, country, region, belts, publisher, grade::text AS grade, state_controlled, wire, kind, url,
           cadence_min, parser, lang, enabled, last_ok_at, fail_count
    FROM sources
    WHERE enabled AND (last_ok_at IS NULL OR last_ok_at + make_interval(mins => cadence_min) <= :due_by)
    ORDER BY last_ok_at NULLS FIRST, id
    """
)


def due_sources(now: datetime | None = None, *, conn: Connection | None = None) -> list[tuple[SourceSpec, SourceState]]:
    """Enabled sources whose cadence has elapsed since `last_ok_at` (or never succeeded), with their state.

    `CADENCE_TOLERANCE` absorbs the seconds between a cron tick and the `now` recorded in `last_ok_at`; without it a
    30-minute source would only run every second tick (45 minutes). Oldest `last_ok_at` first (never-succeeded first),
    so sources that were left unprocessed or truncated by the fetch budget are served before the others.
    """
    with _conn(conn) as c:
        found = c.execute(_DUE, {"due_by": (now or now_utc()) + CADENCE_TOLERANCE}).mappings().all()
    due: list[tuple[SourceSpec, SourceState]] = []
    for row in found:
        spec = SourceSpec.model_validate({k: row[k] for k in SourceSpec.model_fields})
        due.append((spec, SourceState(last_ok_at=row["last_ok_at"], fail_count=row["fail_count"])))
    return due


def source_publishers(*, conn: Connection | None = None) -> dict[str, str]:
    """source_id → publisher for every source (reprint attribution in `WireRules.publishers`)."""
    with _conn(conn) as c:
        return {str(r[0]): str(r[1]) for r in c.execute(text("SELECT id, publisher FROM sources")).all()}


def known_url_hashes(hashes: Iterable[str], *, conn: Connection | None = None) -> set[str]:
    """The subset of `hashes` already present in `raw_documents.url_hash`."""
    wanted = list(hashes)
    if not wanted:
        return set()
    with _conn(conn) as c:
        found = c.execute(text("SELECT url_hash FROM raw_documents WHERE url_hash = ANY(:h)"), {"h": wanted})
        return {str(r[0]).strip() for r in found}


def exists(hash_: str, *, conn: Connection | None = None) -> bool:
    """True if a document (any status, skipped included) with this `url_hash` is stored."""
    with _conn(conn) as c:
        return c.execute(text("SELECT 1 FROM raw_documents WHERE url_hash = :h"), {"h": hash_}).first() is not None


_INSERT_DOC = text(
    """
    INSERT INTO raw_documents
      (id, source_id, url, url_hash, title, published_at, lang, doc_type, snapshot_key, snapshot_sha, text, text_sha,
       simhash, story_id, wire_origin, status, skip_reason)
    VALUES
      (:id, :source_id, :url, :url_hash, :title, :published_at, :lang, CAST(:doc_type AS doc_type), :snapshot_key,
       :snapshot_sha, :text, :text_sha, :simhash, :story_id, :wire_origin, :status, :skip_reason)
    ON CONFLICT (url_hash) DO NOTHING
    """
)


def insert_document(
    doc: Document, *, status: str = "new", skip_reason: str | None = None, conn: Connection | None = None
) -> bool:
    """Insert a document (SimHash stored signed). False if its `url_hash` is already stored (nothing written).

    `skip_reason` is only for `status='skipped'` rows (`too_short`, `lang_undetected`); it is NULL otherwise.
    """
    params = {**doc.model_dump(), "simhash": to_signed(doc.simhash), "status": status, "skip_reason": skip_reason}
    with _conn(conn) as c:
        return c.execute(_INSERT_DOC, params).rowcount == 1


def insert_skipped(raw: RawDoc, error: SkippedDocError, spec: SourceSpec, *, conn: Connection | None = None) -> bool:
    """Record a document that could not be normalized as `status='skipped'`, with the snapshot the error carries.

    The snapshot was stored by `normalize` already; this only writes the row (empty text, simhash 0, no story) so the
    `url_hash` stops the document from being fetched again. `error.reason` is stored in `skip_reason`.
    """
    doc = Document(
        id=error.doc_id,
        source_id=spec.id,
        url=raw.url,
        url_hash=url_hash(raw.url),
        title=raw.title,
        published_at=raw.published_at,
        lang=spec.lang,
        doc_type=doc_type_for(spec),
        text="",
        text_sha=hashlib.sha256(b"").hexdigest(),
        simhash=0,
        snapshot_key=error.snapshot_key,
        snapshot_sha=error.snapshot_sha,
    )
    return insert_document(doc, status="skipped", skip_reason=error.reason, conn=conn)


# Recomputed from every member document on each call, so the row is the same however many times it runs:
# title = best-grade member's title (its first 120 text chars if it has none), n_publishers = distinct
# coalesce(wire_origin, publisher). The representative simhash is the first document's and is never replaced.
_UPSERT_STORY = text(
    """
    WITH members AS (
      SELECT d.id, d.title, d.text, d.lang, d.wire_origin, s.publisher, s.country, s.grade
      FROM raw_documents d JOIN sources s ON s.id = d.source_id
      WHERE d.story_id = :id
    ), best AS (
      SELECT coalesce(nullif(btrim(title), ''), left(text, 120)) AS title FROM members ORDER BY grade, id LIMIT 1
    ), agg AS (
      SELECT count(*)::int AS n_docs,
             count(DISTINCT coalesce(wire_origin, publisher))::int AS n_publishers,
             coalesce(array_agg(DISTINCT lang::text) FILTER (WHERE lang IS NOT NULL), '{}') AS languages,
             coalesce(array_agg(DISTINCT country::text), '{}') AS countries
      FROM members
    )
    INSERT INTO stories (id, first_seen, last_seen, title, simhash, n_docs, n_publishers, languages, countries)
    SELECT :id, :now, :now, best.title, :simhash, agg.n_docs, agg.n_publishers, agg.languages, agg.countries
    FROM best, agg
    ON CONFLICT (id) DO UPDATE SET
      last_seen = GREATEST(stories.last_seen, EXCLUDED.last_seen),
      title = EXCLUDED.title,
      n_docs = EXCLUDED.n_docs,
      n_publishers = EXCLUDED.n_publishers,
      languages = EXCLUDED.languages,
      countries = EXCLUDED.countries
    """
)


def upsert_story(doc: Document, *, now: datetime | None = None, conn: Connection | None = None) -> None:
    """Create or refresh `doc.story_id`'s row from its stored member documents (`doc` must already be inserted)."""
    if doc.story_id is None:
        return
    params = {"id": doc.story_id, "now": now or now_utc(), "simhash": to_signed(doc.simhash)}
    with _conn(conn) as c:
        c.execute(_UPSERT_STORY, params)


def recent_story_heads(
    window: timedelta = STORY_WINDOW, *, now: datetime | None = None, conn: Connection | None = None
) -> list[StoryHead]:
    """Stories that received a document within `window`, as (id, unsigned simhash, last_seen)."""
    cutoff = (now or now_utc()) - window
    with _conn(conn) as c:
        found = c.execute(
            text("SELECT id, simhash, last_seen FROM stories WHERE last_seen >= :cutoff ORDER BY last_seen DESC, id"),
            {"cutoff": cutoff},
        ).all()
    return [StoryHead(id=r[0], simhash=to_unsigned(r[1]), last_seen=r[2]) for r in found]


def story_docs(story_id: str, *, conn: Connection | None = None) -> list[Document]:
    """Every stored document of a story, oldest id first."""
    with _conn(conn) as c:
        found = c.execute(
            text(
                """
                SELECT id, source_id, url, url_hash, title, published_at, lang, doc_type::text AS doc_type, text,
                       text_sha, simhash, snapshot_key, snapshot_sha, story_id, wire_origin
                FROM raw_documents WHERE story_id = :id ORDER BY id
                """
            ),
            {"id": story_id},
        ).mappings()
        return [Document.model_validate({**r, "simhash": to_unsigned(r["simhash"])}) for r in found]


def insert_chunk(chunk: Chunk, *, conn: Connection | None = None) -> bool:
    """Insert a chunk (status 'new'); False if `(doc_id, idx)` already exists."""
    with _conn(conn) as c:
        return (
            c.execute(
                text(
                    """
                    INSERT INTO chunks (id, doc_id, idx, char_start, char_end, text, n_tokens)
                    VALUES (:id, :doc_id, :idx, :char_start, :char_end, :text, :n_tokens)
                    ON CONFLICT (doc_id, idx) DO NOTHING
                    """
                ),
                chunk.model_dump(),
            ).rowcount
            == 1
        )


def set_status(doc_id: str, status: str, *, conn: Connection | None = None) -> None:
    """Set `raw_documents.status` (new|chunked|extracted|skipped|error)."""
    with _conn(conn) as c:
        c.execute(text("UPDATE raw_documents SET status = :status WHERE id = :id"), {"status": status, "id": doc_id})


def mark_ok(spec: SourceSpec, *, at: datetime | None = None, conn: Connection | None = None) -> None:
    """A crawl succeeded: `last_ok_at = at` (the crawl's start, so the next `since` leaves no gap), streak reset."""
    with _conn(conn) as c:
        c.execute(
            text("UPDATE sources SET last_ok_at = :at, last_error = NULL, fail_count = 0 WHERE id = :id"),
            {"at": at or now_utc(), "id": spec.id},
        )


def mark_fail(spec: SourceSpec, error: str = "", *, conn: Connection | None = None) -> int:
    """A crawl failed: `fail_count += 1`, record the error, and disable the source at 5 in a row. Returns the count."""
    with _conn(conn) as c:
        row = c.execute(
            text(
                """
                UPDATE sources
                SET fail_count = fail_count + 1,
                    last_error = :error,
                    enabled = enabled AND fail_count + 1 < :max_fails
                WHERE id = :id
                RETURNING fail_count, enabled
                """
            ),
            {"id": spec.id, "error": error[:500] or None, "max_fails": MAX_FAILS},
        ).one()
    fail_count, enabled = int(row[0]), bool(row[1])
    if not enabled:
        log.warning("source_disabled", source_id=spec.id, fail_count=fail_count, last_error=error[:200])
    return fail_count
