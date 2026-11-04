"""extract flow: classify → LLM extract → validate → `extraction_runs` per new chunk (code-phase0.md §2.6)."""

from __future__ import annotations

from prefect import flow

from domino_core.clients.db import connection
from domino_core.errors import BudgetExceeded, TransientError
from domino_core.ids import new_id
from domino_core.log import bind_context, get_logger
from domino_core.settings import get_settings
from domino_extract import store
from domino_extract.classifier import classify
from domino_extract.extractor import Extracted, ExtractFailed, extract
from domino_extract.schema import PROMPT_VERSION

CHUNK_LIMIT = 600  # chunks looked at per run (W2-9)


def _record(chunk_id: str, outcome: Extracted | ExtractFailed, model: str) -> None:
    """The run row, its error rows and the chunk's new status in one transaction (a crash leaves none of them)."""
    with connection() as conn:
        run_id = store.record_run(chunk_id, outcome, model=model, conn=conn)
        if run_id is not None:
            store.record_errors(chunk_id, run_id, outcome, conn=conn)
        status = "extracted" if isinstance(outcome, Extracted) else "error"
        store.set_chunk_status(chunk_id, status, conn=conn)


@flow(name="extract", timeout_seconds=1200)
def extract_flow() -> None:
    """Extract triples from every new en/ko chunk that has no run for this prompt version; stop at a budget or limit.

    Does nothing while `EXTRACT_ENABLED` is off (the default). `resolve` and `graph` come in Week 3 (W2-9), so a chunk
    ends as `extracted`, `dropped` or `error` after its `extraction_runs` row is written.
    """
    log = get_logger("flows.extract")  # bound per run so the stderr target is current (tests redirect it)
    settings = get_settings()
    if not settings.extract_enabled:
        log.info("extract_disabled", hint="set EXTRACT_ENABLED=true once the LLM authentication is decided")
        return
    with bind_context(run_id=new_id("run"), flow="extract"):
        pending = store.new_chunks(limit=CHUNK_LIMIT)
        log.info("extract_start", n_chunks=len(pending), prompt_version=PROMPT_VERSION)
        counts = {"extracted": 0, "dropped": 0, "error": 0, "skipped": 0}
        stopped: str | None = None
        for p in pending:
            chunk = p.chunk
            with bind_context(key=chunk.id):
                if store.run_exists(chunk.id, PROMPT_VERSION):
                    counts["skipped"] += 1
                    continue
                try:
                    is_causal = p.is_causal
                    if is_causal is None:
                        is_causal = classify(chunk, p.lang)
                        store.set_is_causal(chunk.id, is_causal)  # kept, so a stopped run does not classify twice
                    if not is_causal:
                        store.set_chunk_status(chunk.id, "dropped")
                        counts["dropped"] += 1
                        continue
                    outcome: Extracted | ExtractFailed
                    try:
                        outcome = extract(chunk, p.src, lang=p.lang, published_at=p.published_at)
                    except ExtractFailed as e:  # twice rejected: recorded as a failed run, then on to the next chunk
                        outcome = e
                except (
                    BudgetExceeded
                ) as e:  # before TransientError/PermanentError (W2-9): the rest waits for the budget
                    stopped = f"budget_exceeded: {e}"
                    break
                except TransientError as e:  # incl. UsageLimitReached: this chunk stays as it was, the next run goes on
                    stopped = f"{type(e).__name__}: {e}"
                    break
                _record(chunk.id, outcome, settings.model_extract)
                counts["extracted" if isinstance(outcome, Extracted) else "error"] += 1
        if stopped is not None:
            log.warning("extract_stopped", reason=stopped, **counts)
        log.info("extract_done", n_chunks=len(pending), **counts)


if __name__ == "__main__":
    extract_flow()
