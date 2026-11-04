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
from domino_ingest.crawlers.base import fetch_budget, skip_known
from domino_ingest.normalizer import SkippedDocError, normalize, url_hash
from domino_ingest.sources import WIRES_FILE, load_wires
from domino_ingest.store import SourceState
from domino_ingest.story import STORY_WINDOW, WireRules, assign_story, detect_wire

ROOT = Path(__file__).resolve().parents[1]
FIRST_LOOKBACK = timedelta(hours=24)  # `since` for a source that has never succeeded

FETCH_ATTEMPTS = 3  # crawl attempts per source and run on TransientError (tech §5 ingest.fetch)
FETCH_BACKOFF_S = 2.0  # wait 2 s, then 4 s, between attempts

# Fetch time budgets (decision W2-1, review H2). A Crawl-delay-30 source (Destatis) after a long pause has hours of
# backlog; without a budget the flow hits its 600 s limit, stores nothing and starves the sources after it.
SOURCE_FETCH_BUDGET_S = 120  # one source's crawl, retries included
FLOW_FETCH_BUDGET_S = 450  # the whole run: the 600 s limit minus 150 s for storing and the extract trigger
MIN_SOURCE_BUDGET_S = 10  # with less than this left, the remaining sources are not started (they stay due)

log = get_logger("flows.ingest")


def _known_urls(urls: list[str]) -> set[str]:
    """Which of these URLs are already stored (crawlers do not download those again)."""
    hashes = {u: url_hash(u) for u in urls}
    known = store.known_url_hashes(hashes.values())
    return {u for u, h in hashes.items() if h in known}


def _crawl(spec: SourceSpec, since: datetime, budget_s: float) -> tuple[list[RawDoc], bool]:
    """One source's crawl: (documents, truncated). `truncated` means the fetch budget ran out with entries left over.

    TransientError is retried with exponential backoff, PermanentError never. All attempts share one `budget_s`; once
    it is spent a failed attempt is not retried (the retry would be truncated at its first download).
    """
    if spec.parser not in CRAWLERS:
        raise PermanentError(f"unknown crawler {spec.parser!r}")
    with fetch_budget(budget_s) as budget:
        for attempt in range(1, FETCH_ATTEMPTS + 1):
            try:
                with skip_known(_known_urls):
                    return CRAWLERS[spec.parser]().fetch(spec, since=since), budget.truncated
            except TransientError as e:
                if attempt == FETCH_ATTEMPTS or budget.expired():
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
        settings = get_settings()
        doc.story_id = assign_story(doc, heads, settings.simhash_threshold, now=now)
        # a wire agency's own documents carry its wires.yaml key, so original and reprints aggregate under one key (H3)
        doc.wire_origin = spec.wire or detect_wire(doc, store.story_docs(doc.story_id, conn=conn), wires)
        if not store.insert_document(doc, conn=conn):
            return "duplicate"
        store.upsert_story(doc, now=now, conn=conn)
        for c in chunk(doc, max_tokens=settings.chunk_tokens, overlap=settings.chunk_overlap):
            store.insert_chunk(c, conn=conn)
        store.set_status(doc.id, "chunked", conn=conn)
    return "chunked"


def _ingest_source(spec: SourceSpec, state: SourceState, now: datetime, wires: WireRules, budget_s: float) -> None:
    since = state.last_ok_at or now - FIRST_LOOKBACK
    try:
        raws, truncated = _crawl(spec, since, budget_s)
    except Exception as e:  # Domino errors and library exceptions alike: only this source is skipped (L3)
        if not isinstance(e, DominoError):
            log.exception("source_crashed", error=str(e))
        fail_count = store.mark_fail(spec, f"{type(e).__name__}: {e}")
        log.warning("source_failed", error=str(e), fail_count=fail_count)
        return
    counts = {"chunked": 0, "skipped": 0, "duplicate": 0, "failed": 0, "known": 0}
    failed: list[RawDoc] = []
    for raw in raws:
        if store.exists(url_hash(raw.url)):  # idempotent: the same url_hash is never stored twice
            counts["known"] += 1
            continue
        try:
            counts[_ingest_document(raw, spec, now, wires)] += 1
        except TransientError:  # storage outage: not this document's fault, `last_ok_at` stays where it was
            raise
        except Exception as e:  # PermanentError or a library exception: this document only; its snapshot, if any, stays
            counts["failed"] += 1
            failed.append(raw)
            log.warning("document_failed", url=raw.url, error_type=type(e).__name__, error=str(e))
    # `last_ok_at` is the run's start, so the next `since` overlaps the crawl instead of leaving a gap. A failed
    # document holds it back to its own publication date, so the next run fetches that entry again. (`fail_count`
    # counts source failures only.)
    mark_at = min([r.published_at for r in failed if r.published_at is not None] + [now])
    if truncated:
        # The fetch budget ran out with entries left: what was fetched is stored above, but the source must not move
        # past what it still owes, or those entries are lost for good. Next run starts from the same `since` and
        # `skip_known` skips what is stored, so progress only goes forward. With a failed document too, the older wins.
        mark_at = min(mark_at, since)
        log.warning("source_truncated", n_fetched=len(raws), last_ok_at=mark_at.isoformat())
    store.mark_ok(spec, at=mark_at)
    log.info("source_ok", n_fetched=len(raws), truncated=truncated, last_ok_at=mark_at.isoformat(), **counts)


@flow(name="ingest", timeout_seconds=600)
def ingest_flow(now: datetime | None = None) -> None:
    """Fetch every due source (longest-waiting first) within the time budgets; store documents with story and chunks."""
    started = base.CLOCK.monotonic()
    now = now or now_utc()
    run_id = new_id("run")
    with bind_context(run_id=run_id, flow="ingest"):
        wires = WireRules(patterns=load_wires(ROOT / "sources" / WIRES_FILE), publishers=store.source_publishers())
        due = store.due_sources(now)
        log.info("ingest_start", n_due=len(due))
        for n_done, (spec, state) in enumerate(due):
            left = FLOW_FETCH_BUDGET_S - (base.CLOCK.monotonic() - started)
            if left < MIN_SOURCE_BUDGET_S:
                # not started, state untouched: they stay due and, having waited longest, go first next run
                log.warning("flow_budget_spent", n_untouched=len(due) - n_done, n_due=len(due))
                break
            with bind_context(key=spec.id):
                _ingest_source(spec, state, now, wires, min(SOURCE_FETCH_BUDGET_S, left))
        log.info("ingest_done", n_due=len(due))


if __name__ == "__main__":
    ingest_flow()
