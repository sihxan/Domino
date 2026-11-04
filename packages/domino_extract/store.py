"""Postgres store for extraction: chunk queue, `is_causal`/status updates, `extraction_runs`, `extraction_errors`."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import Connection, text

from domino_core.clients.db import connection
from domino_core.errors import PermanentError
from domino_core.ids import new_id
from domino_core.models import Chunk, SourceSpec
from domino_extract.classifier import EXTRACT_LANGS
from domino_extract.extractor import Extracted, ExtractFailed
from domino_extract.schema import PROMPT_VERSION

CHUNK_STATUSES = frozenset({"new", "extracted", "dropped", "error"})  # chunks.status (tech section 3.1)
DEFAULT_LIMIT = 600


@dataclass(frozen=True)
class PendingChunk:
    """A chunk waiting for extraction with what the pipeline needs next to it: source, document language and date."""

    chunk: Chunk
    src: SourceSpec
    lang: str  # raw_documents.lang (the language of the text; `src.lang` is the source's nominal one)
    published_at: datetime | None
    is_causal: bool | None  # an earlier classification, so the classifier is not asked twice (W2-9)


@contextmanager
def _conn(conn: Connection | None) -> Iterator[Connection]:
    """The caller's connection (one transaction across several calls), or a fresh transactional one."""
    if conn is not None:
        yield conn
    else:
        with connection() as new:
            yield new


_NEW_CHUNKS = text(
    """
    SELECT c.id AS c_id, c.doc_id AS c_doc_id, c.idx AS c_idx, c.char_start AS c_char_start,
           c.char_end AS c_char_end, c.text AS c_text, c.n_tokens AS c_n_tokens, c.is_causal AS c_is_causal,
           d.lang::text AS d_lang, d.published_at AS d_published_at,
           s.id AS s_id, s.country AS s_country, s.region AS s_region, s.belts AS s_belts,
           s.publisher AS s_publisher, s.grade::text AS s_grade, s.state_controlled AS s_state_controlled,
           s.wire AS s_wire, s.kind AS s_kind, s.url AS s_url, s.cadence_min AS s_cadence_min,
           s.parser AS s_parser, s.lang AS s_lang, s.enabled AS s_enabled
    FROM chunks c
    JOIN raw_documents d ON d.id = c.doc_id
    JOIN sources s ON s.id = d.source_id
    WHERE c.status = 'new' AND d.status = 'chunked' AND d.lang::text = ANY(:langs)
    ORDER BY c.id
    LIMIT :limit
    """
)


def new_chunks(limit: int = DEFAULT_LIMIT, *, conn: Connection | None = None) -> list[PendingChunk]:
    """Up to `limit` chunks to extract, oldest id first (= collection order, so none starves).

    Only `status='new'` chunks of `chunked` documents whose language is in `EXTRACT_LANGS` (W2-10); other languages
    stay `new` with `is_causal` NULL.
    """
    with _conn(conn) as c:
        found = c.execute(_NEW_CHUNKS, {"langs": list(EXTRACT_LANGS), "limit": limit}).mappings().all()
    pending: list[PendingChunk] = []
    for r in found:
        chunk = Chunk(
            id=r["c_id"],
            doc_id=r["c_doc_id"],
            idx=r["c_idx"],
            char_start=r["c_char_start"],
            char_end=r["c_char_end"],
            text=r["c_text"],
            n_tokens=r["c_n_tokens"],
        )
        src = SourceSpec.model_validate({k: r[f"s_{k}"] for k in SourceSpec.model_fields})
        pending.append(PendingChunk(chunk, src, r["d_lang"], r["d_published_at"], r["c_is_causal"]))
    return pending


def run_exists(chunk_id: str, prompt_version: str = PROMPT_VERSION, *, conn: Connection | None = None) -> bool:
    """True if an `extraction_runs` row exists for the idempotency key `(chunk_id, prompt_version)`."""
    with _conn(conn) as c:
        row = c.execute(
            text("SELECT 1 FROM extraction_runs WHERE chunk_id = :c AND prompt_version = :p"),
            {"c": chunk_id, "p": prompt_version},
        ).first()
    return row is not None


def set_is_causal(chunk_id: str, is_causal: bool, *, conn: Connection | None = None) -> None:
    """Store the classifier's answer in `chunks.is_causal`."""
    with _conn(conn) as c:
        c.execute(text("UPDATE chunks SET is_causal = :v WHERE id = :id"), {"v": is_causal, "id": chunk_id})


def set_chunk_status(chunk_id: str, status: str, *, conn: Connection | None = None) -> None:
    """Set `chunks.status` (new|extracted|dropped|error)."""
    if status not in CHUNK_STATUSES:
        raise PermanentError(f"invalid chunk status {status!r}")
    with _conn(conn) as c:
        c.execute(text("UPDATE chunks SET status = :s WHERE id = :id"), {"s": status, "id": chunk_id})


def _raw_json(outcome: Extracted | ExtractFailed) -> Any:
    """`extraction_runs.raw_json`: the model output as a JSON object; `{"raw": text}` if it is not one."""
    if isinstance(outcome, Extracted):
        return json.loads(outcome.raw)
    if outcome.raw is None:
        return {"raw": None, "error": outcome.reason}
    try:
        parsed = json.loads(outcome.raw)
    except ValueError:
        return {"raw": outcome.raw}
    return parsed if isinstance(parsed, dict) else {"raw": outcome.raw}


_INSERT_RUN = text(
    """
    INSERT INTO extraction_runs
      (id, chunk_id, model, prompt_version, input_tokens, output_tokens, cost_usd, n_triples, n_dropped, raw_json)
    VALUES
      (:id, :chunk_id, :model, :prompt_version, :input_tokens, :output_tokens, :cost_usd, :n_triples, :n_dropped,
       CAST(:raw_json AS jsonb))
    ON CONFLICT (chunk_id, prompt_version) DO NOTHING
    RETURNING id
    """
)


def record_run(
    chunk_id: str,
    outcome: Extracted | ExtractFailed,
    *,
    model: str,
    prompt_version: str = PROMPT_VERSION,
    conn: Connection | None = None,
) -> str | None:
    """Insert the `extraction_runs` row for this chunk and prompt version; its id, or None if one already exists.

    A failed extraction is recorded too (0 triples, the last raw output) so the chunk is not paid for again under the
    same prompt version (W2-11). Idempotent: a second call writes nothing.
    """
    if isinstance(outcome, Extracted):
        n_triples, n_dropped = len(outcome.triples), len(outcome.drops)
    else:
        n_triples, n_dropped = 0, outcome.n_dropped
    params = {
        "id": new_id("run"),
        "chunk_id": chunk_id,
        "model": model,
        "prompt_version": prompt_version,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "cost_usd": outcome.cost_usd,
        "n_triples": n_triples,
        "n_dropped": n_dropped,
        "raw_json": json.dumps(_raw_json(outcome), ensure_ascii=False),
    }
    with _conn(conn) as c:
        row = c.execute(_INSERT_RUN, params).first()
    return None if row is None else str(row[0])


def record_errors(
    chunk_id: str, run_id: str, outcome: Extracted | ExtractFailed, *, conn: Connection | None = None
) -> int:
    """Insert one `extraction_errors` row per dropped triple (stage `grounding`|`validator`), or one for a failed run.

    Call it only with the id `record_run` returned (None means the rows were written before). Returns the row count.
    """
    if isinstance(outcome, Extracted):
        entries = [
            {
                "stage": "grounding" if kind == "grounding" else "validator",
                "reason": kind,
                "payload": json.dumps(t.model_dump(mode="json"), ensure_ascii=False),
            }
            for kind, t in outcome.drops
        ]
    else:
        entries = [
            {
                "stage": outcome.stage,
                "reason": outcome.reason,
                "payload": json.dumps({"n_dropped": outcome.n_dropped}),
            }
        ]
    if not entries:
        return 0
    with _conn(conn) as c:
        c.execute(
            text(
                """
                INSERT INTO extraction_errors (chunk_id, run_id, stage, reason, payload)
                VALUES (:chunk_id, :run_id, :stage, :reason, CAST(:payload AS jsonb))
                """
            ),
            [{"chunk_id": chunk_id, "run_id": run_id, **e} for e in entries],
        )
    return len(entries)
