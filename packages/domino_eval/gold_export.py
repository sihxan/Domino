"""Export collected chunks as an unlabeled gold jsonl for a human labeler: `python -m domino_eval.gold_export`.

Reads Postgres only. Labels stay empty (`has_causal` null): gold labels are written by a person, never by a model
(decision W2-13), and `load_gold` refuses a file that still has empty labels.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import text

from domino_core.clients.db import connection
from domino_core.errors import PermanentError

_ROWS = text(
    """
    SELECT c.id AS chunk_id, c.doc_id AS doc_id, d.source_id AS source_id, d.lang::text AS lang, c.text AS text,
           s.publisher AS publisher, s.grade::text AS grade, s.state_controlled AS state_controlled
    FROM chunks c
    JOIN raw_documents d ON d.id = c.doc_id
    JOIN sources s ON s.id = d.source_id
    WHERE d.lang::text = :lang AND d.status = 'chunked'
    """
)


@dataclass(frozen=True)
class ExportRow:
    """A stored chunk with the document and source facts a labeler needs."""

    chunk_id: str
    doc_id: str
    source_id: str
    lang: str
    text: str
    publisher: str
    grade: str
    state_controlled: bool


def fetch_rows(lang: str) -> list[ExportRow]:
    """All chunks of `chunked` documents in `lang` (one SELECT)."""
    with connection() as conn:
        found = conn.execute(_ROWS, {"lang": lang}).mappings().all()
    return [ExportRow(**dict(r)) for r in found]


def _round_robin(rows: Sequence[ExportRow], n: int) -> list[ExportRow]:
    queues: dict[str, deque[ExportRow]] = {}
    for r in rows:
        queues.setdefault(r.source_id, deque()).append(r)
    out: list[ExportRow] = []
    while len(out) < n and any(queues.values()):
        for source_id in sorted(queues):
            if queues[source_id] and len(out) < n:
                out.append(queues[source_id].popleft())
    return out


def select_chunks(rows: Sequence[ExportRow], n: int) -> list[ExportRow]:
    """Up to `n` chunks, spread over sources, at most one per document until every document has one.

    Overlapping chunks of one document would be near-duplicate gold items, so the first pass takes one chunk per
    document and a second pass fills up from the rest. The pick is a deterministic function of the chunk ids
    (hash order), not of the input order, so the same database gives the same file.
    """
    ordered = sorted(rows, key=lambda r: hashlib.sha256(r.chunk_id.encode()).hexdigest())
    seen_docs: set[str] = set()
    first: list[ExportRow] = []
    rest: list[ExportRow] = []
    for r in ordered:
        (rest if r.doc_id in seen_docs else first).append(r)
        seen_docs.add(r.doc_id)
    picked = _round_robin(first, n)
    if len(picked) < n:
        picked += _round_robin(rest, n - len(picked))
    return picked


def blank_item(row: ExportRow, chunk_id: str) -> dict[str, Any]:
    """The gold layout of code section 7.1 with the labels left empty."""
    return {
        "chunk_id": chunk_id,
        "lang": row.lang,
        "text": row.text,
        "source": {"publisher": row.publisher, "grade": row.grade, "state_controlled": row.state_controlled},
        "labels": {"has_causal": None, "triples": []},
        "annotators": [],
        "kappa": None,
    }


def write_items(path: Path, items: Sequence[dict[str, Any]]) -> None:
    """Write one JSON object per line (UTF-8, Korean as is); an existing file is never overwritten."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as f:
            for item in items:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
    except FileExistsError:
        raise PermanentError(f"gold_export: {path} already exists; labels in it are never overwritten") from None


def main(argv: Sequence[str] | None = None) -> int:
    """CLI: `--lang en|ko --n 15 --out PATH`; prints what was written to stderr."""
    ap = argparse.ArgumentParser(description="Export unlabeled gold candidates from the chunks table.")
    ap.add_argument("--lang", required=True, choices=["en", "ko"])
    ap.add_argument("--n", type=int, default=15, help="number of chunks to export (default 15)")
    ap.add_argument("--out", required=True, type=Path, help="new jsonl file; must not exist")
    args = ap.parse_args(argv)
    try:
        if args.n < 1:
            raise PermanentError("gold_export: --n must be at least 1")
        if args.out.exists():
            raise PermanentError(f"gold_export: {args.out} already exists; labels in it are never overwritten")
        rows = fetch_rows(args.lang)
        picked = select_chunks(rows, args.n)
        write_items(args.out, [blank_item(r, f"gold_{r.chunk_id}") for r in picked])
    except PermanentError as e:
        print(str(e), file=sys.stderr)
        return 2
    sources = len({r.source_id for r in picked})
    print(
        f"wrote {len(picked)} of {len(rows)} {args.lang} chunks ({sources} sources) to {args.out}; "
        "fill labels.has_causal, labels.triples and annotators by hand",
        file=sys.stderr,
    )
    if len(picked) < args.n:
        print(f"only {len(picked)} chunks available (asked for {args.n})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
