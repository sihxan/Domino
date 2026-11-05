"""Gold set format (code section 7.1): `GoldItem` models and the jsonl loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from domino_core.errors import PermanentError
from domino_extract.schema import CausalType, ClaimType, Direction, Horizon, RelKind


class _Strict(BaseModel):
    """Hand-written labels: an unknown key is a typo, not something to drop silently."""

    model_config = ConfigDict(extra="forbid")


class GoldSource(_Strict):
    """Who published the chunk (what the extractor's user message shows)."""

    publisher: str
    grade: Literal["A", "B", "C"]
    state_controlled: bool = False


class GoldTriple(_Strict):
    """One labeled cause -> effect statement; names are copied from the text like the extractor's."""

    cause: str
    effect: str
    direction_effect: Direction
    relation_kind: RelKind
    causal_type: CausalType
    horizon: Horizon
    claim_type: ClaimType
    quote: str


class GoldLabels(_Strict):
    """`has_causal` is the Task-1 label; `triples` is empty exactly when it is false."""

    has_causal: bool
    triples: list[GoldTriple]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.has_causal and not self.triples:
            raise ValueError("has_causal is true but no triple is labeled")
        if not self.has_causal and self.triples:
            raise ValueError("has_causal is false but triples are labeled")
        return self


class GoldItem(_Strict):
    """One labeled chunk. `kappa` is None for a single annotator (decision W2-13)."""

    chunk_id: str
    lang: Literal["en", "ko"]
    text: str
    source: GoldSource
    labels: GoldLabels
    annotators: list[str] = Field(min_length=1)
    kappa: float | None = None


def _line_error(path: Path, lineno: int, detail: str) -> PermanentError:
    return PermanentError(f"gold: {path} line {lineno}: {detail}")


def _unlabeled(raw: object) -> bool:
    if not isinstance(raw, dict):
        return False
    labels = raw.get("labels")
    return isinstance(labels, dict) and labels.get("has_causal") is None


def load_gold(path: str | Path) -> list[GoldItem]:
    """Read a gold jsonl file (blank lines skipped); every problem is a `PermanentError` naming the line.

    Rejects unlabeled items (`has_causal` null, as `gold_export` writes them), duplicate `chunk_id`s, unknown keys and
    labels that contradict each other.
    """
    p = Path(path)
    if not p.is_file():
        raise PermanentError(f"gold: file not found: {p}")
    items: list[GoldItem] = []
    seen: set[str] = set()
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError as e:
            raise _line_error(p, lineno, f"not valid JSON ({e})") from e
        if _unlabeled(raw):
            raise _line_error(p, lineno, "unlabeled item (labels.has_causal is null)")
        try:
            item = GoldItem.model_validate(raw)
        except ValidationError as e:
            first = e.errors()[0]
            where = ".".join(str(x) for x in first["loc"])
            raise _line_error(p, lineno, f"{where}: {first['msg']}") from e
        if item.chunk_id in seen:
            raise _line_error(p, lineno, f"duplicate chunk_id {item.chunk_id!r}")
        seen.add(item.chunk_id)
        items.append(item)
    return items
