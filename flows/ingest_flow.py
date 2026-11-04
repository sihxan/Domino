"""ingest flow: crawl due sources → normalize → story → chunk → Postgres (code-phase0.md §1.6, reviewed version)."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from prefect import flow

from domino_core.clients.db import connection
from domino_core.errors import DominoError, PermanentError, TransientError
from domino_core.ids import new_id
from domino_core.log import bind_context, get_logger
from domino_core.models import RawDoc, SourceSpec
from domino_core.settings import get_settings
from domino_core.timeutil import now_utc
from domino_ingest import store
from domino_ingest.chunker import chunk
from domino_ingest.crawlers import CRAWLERS, base
from domino_ingest.crawlers.base import skip_known
from domino_ingest.normalizer import SkippedDocError, normalize, url_hash
from domino_ingest.sources import WIRES_FILE, load_wires
from domino_ingest.store import SourceState
from domino_ingest.story import STORY_WINDOW, WireRules, assign_story, detect_wire

ROOT = Path(__file__).resolve().parents[1]
FIRST_LOOKBACK = timedelta(hours=24)  # `since` for a source that has never succeeded

FETCH_ATTEMPTS = 3  # crawl attempts per source and run on TransientError (tech §5 ingest.fetch)
FETCH_BACKOFF_S = 2.0  # wait 2 s, then 4 s, between attempts

log = get_logger("flows.ingest")


def _known_urls(urls: list[str]) -> set[str]:
    """Which of these URLs are already stored (crawlers do not download those again)."""
    hashes = {u: url_hash(u) for u in urls}
    known = store.known_url_hashes(hashes.values())
    return {u for u, h in hashes.items() if h in known}


def _crawl(spec: SourceSpec, since: datetime) -> list[RawDoc]:
    """One source's crawl, retried on TransientError with exponential backoff; PermanentError is never retried."""
    if spec.parser not in CRAWLERS:
        raise PermanentError(f"unknown crawler {spec.parser!r}")
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            with skip_known(_known_urls):
                return CRAWLERS[spec.parser]().fetch(spec, since=since)
        except TransientError as e:
            if attempt == FETCH_ATTEMPTS:
                raise
            delay = FETCH_BACKOFF_S * 2 ** (attempt - 1)
            log.warning("fetch_retry", attempt=attempt, wait_s=delay, error=str(e))
            base.CLOCK.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


def _ingest_document(raw: RawDoc, spec: SourceSpec, now: datetime, wires: WireRules) -> str:
    """Normalize and store one fetched document. Returns 'chunked', 'skipped' or 'duplicate'."""
    try:
        doc = normalize(raw, spec)
    except SkippedDocError as e:
        store.insert_skipped(raw, e, spec)
        return "skipped"
    # One transaction per document: a crash can never leave a stored document without its story and chunks.
    with connection() as conn:
        heads = store.recent_story_heads(STORY_WINDOW, now=now, conn=conn)
        doc.story_id = assign_story(doc, heads, get_settings().simhash_threshold, now=now)
        doc.wire_origin = detect_wire(doc, store.story_docs(doc.story_id, conn=conn), wires)
        if not store.insert_document(doc, conn=conn):
            return "duplicate"
        store.upsert_story(doc, now=now, conn=conn)
        for c in chunk(doc):
            store.insert_chunk(c, conn=conn)
        store.set_status(doc.id, "chunked", conn=conn)
    return "chunked"


def _ingest_source(spec: SourceSpec, state: SourceState, now: datetime, wires: WireRules) -> None:
    try:
        raws = _crawl(spec, state.last_ok_at or now - FIRST_LOOKBACK)
    except DominoError as e:  # Transient and Permanent alike: only this source is skipped
        fail_count = store.mark_fail(spec, f"{type(e).__name__}: {e}")
        log.warning("source_failed", error=str(e), fail_count=fail_count)
        return
    counts = {"chunked": 0, "skipped": 0, "duplicate": 0, "failed": 0, "known": 0}
    for raw in raws:
        if store.exists(url_hash(raw.url)):  # idempotent: the same url_hash is never stored twice
            counts["known"] += 1
            continue
        try:
            counts[_ingest_document(raw, spec, now, wires)] += 1
        except PermanentError as e:  # this document only (e.g. an invalid page); the snapshot, if any, stays
            counts["failed"] += 1
            log.warning("document_failed", url=raw.url, error=str(e))
    # `last_ok_at` is the run's start: the next `since` overlaps the crawl instead of leaving a gap.
    store.mark_ok(spec, at=now)
    log.info("source_ok", n_fetched=len(raws), **counts)


@flow(name="ingest", timeout_seconds=600)
def ingest_flow(now: datetime | None = None) -> None:
    """Fetch every due source, store new documents with story and chunks. (extract_flow is added in Week 2.)"""
    now = now or now_utc()
    run_id = new_id("run")
    with bind_context(run_id=run_id, flow="ingest"):
        wires = WireRules(patterns=load_wires(ROOT / "sources" / WIRES_FILE), publishers=store.source_publishers())
        due = store.due_sources(now)
        log.info("ingest_start", n_due=len(due))
        for spec, state in due:
            with bind_context(key=spec.id):
                _ingest_source(spec, state, now, wires)
        log.info("ingest_done", n_due=len(due))


if __name__ == "__main__":
    ingest_flow()
