"""LLM extraction of one chunk: user-message `wrap`, one reprompt on a schema/grounding failure, `ExtractFailed`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from domino_core.clients import default_settings, llm
from domino_core.errors import BudgetExceeded, PermanentError
from domino_core.log import get_logger
from domino_core.models import Chunk, SourceSpec
from domino_core.timeutil import report_date
from domino_extract.prompts import EXTRACT_SYSTEM
from domino_extract.schema import PROMPT_VERSION, ExtractionResult
from domino_extract.validator import DropReason, ValidatedTriple, validate

log = get_logger(__name__)

Stage = Literal["schema", "grounding"]

RETRY_SUFFIX = (
    "\n\nYour previous output was rejected: {reason}. "
    "Return only JSON valid against the schema; every quote must be a verbatim substring."
)
REASON_MAX_CHARS = 300


class ExtractFailed(PermanentError):
    """Both attempts failed the schema or the grounding gate: record the run with 0 triples and mark the chunk."""

    def __init__(
        self,
        stage: Stage,
        raw: str | None,
        *,
        reason: str | None = None,
        n_dropped: int = 0,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        self.stage = stage
        self.raw = raw  # the last raw model output; None when `llm.call` raised before returning one (schema)
        self.reason = reason or f"extraction failed at stage {stage}"
        self.n_dropped = n_dropped  # triple count of the last result (W2-11), 0 when there was none
        self.input_tokens = input_tokens  # None / None / None: no usage was reported by any attempt
        self.output_tokens = output_tokens
        self.cost_usd = cost_usd
        super().__init__(self.reason)


@dataclass
class Extracted:
    """A successful extraction: validated triples, validator drops, usage of the attempts (summed)."""

    triples: list[ValidatedTriple]
    drops: list[DropReason]
    parsed: ExtractionResult
    raw: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cache_read: int
    attempts: int


def _attr(value: str) -> str:
    return value.replace('"', "&quot;")


def wrap(chunk: Chunk, src: SourceSpec, *, lang: str, published_at: datetime | None) -> str:
    """The user message of code section 2.2; the chunk text is inserted verbatim (quotes are checked against it)."""
    published = published_at.isoformat() if published_at is not None else "unknown"
    return (
        f'<document source="{_attr(src.publisher)}" grade="{src.grade}" published_at="{published}" lang="{lang}" '
        f'state_controlled="{src.state_controlled}">\n{chunk.text}\n</document>'
    )


def _call(user: str) -> llm.LLMResult[ExtractionResult]:
    return llm.call(
        task="extract",
        model=default_settings().model_extract,
        system=EXTRACT_SYSTEM,
        user=user,
        schema=ExtractionResult,
        budget_key=f"budget:extract:{report_date().isoformat()}",
        trace={"prompt_version": PROMPT_VERSION},
    )


def _stage_of(error: PermanentError) -> Stage | None:
    """The failure stage if `error` is about this chunk's output; None for configuration/infrastructure errors."""
    msg = str(error)
    if msg == "grounding_rate_low":
        return "grounding"
    if msg.startswith(("schema:", "refusal:")):
        return "schema"
    return None


def _one_line(reason: str) -> str:
    return " ".join(reason.split())[:REASON_MAX_CHARS]


def extract(chunk: Chunk, src: SourceSpec, *, lang: str, published_at: datetime | None) -> Extracted:
    """Extract and validate one chunk; on a schema or `grounding_rate_low` failure reprompt once, then `ExtractFailed`.

    `BudgetExceeded`, `TransientError` (`UsageLimitReached` included) and PermanentErrors that are not about the
    output (unknown model, missing key, ...) propagate unchanged so the caller can stop the run.
    """
    user = wrap(chunk, src, lang=lang, published_at=published_at)
    first: llm.LLMResult[ExtractionResult] | None = None
    try:
        first = _call(user)
        triples, drops = validate(first.parsed, chunk, src)
    except BudgetExceeded:
        raise
    except PermanentError as e:
        return retry_once_or_drop(chunk, src, user, e, prior=first)
    return Extracted(
        triples=triples,
        drops=drops,
        parsed=first.parsed,
        raw=first.raw,
        input_tokens=first.input_tokens,
        output_tokens=first.output_tokens,
        cost_usd=first.cost_usd,
        cache_read=first.cache_read,
        attempts=1,
    )


def retry_once_or_drop(
    chunk: Chunk,
    src: SourceSpec,
    user: str,
    error: PermanentError,
    *,
    prior: llm.LLMResult[ExtractionResult] | None,
) -> Extracted:
    """Call again once with a one-line rejection reason appended to `user` (same system, so the cache holds).

    `prior` is the first attempt's result when it returned one (grounding failure); its usage is added to the second
    attempt's. A second schema/grounding failure raises `ExtractFailed`; an error that is not about the output
    (including `BudgetExceeded`) is re-raised as is (W2-9, W2-11).
    """
    if isinstance(error, BudgetExceeded) or _stage_of(error) is None:
        raise error
    log.warning("extract_reprompt", chunk_id=chunk.id, reason=_one_line(str(error)))
    second: llm.LLMResult[ExtractionResult] | None = None
    try:
        second = _call(user + RETRY_SUFFIX.format(reason=_one_line(str(error))))
        triples, drops = validate(second.parsed, chunk, src)
    except BudgetExceeded:
        raise
    except PermanentError as e:
        stage = _stage_of(e)
        if stage is None:
            raise
        spent = [r for r in (prior, second) if r is not None]
        log.warning("extract_failed", chunk_id=chunk.id, stage=stage, reason=_one_line(str(e)))
        raise ExtractFailed(
            stage,
            second.raw if second is not None else None,
            reason=_one_line(str(e)),
            n_dropped=len(second.parsed.triples) if second is not None else 0,
            input_tokens=sum(r.input_tokens for r in spent) if spent else None,
            output_tokens=sum(r.output_tokens for r in spent) if spent else None,
            cost_usd=sum(r.cost_usd for r in spent) if spent else None,
        ) from e
    results = [r for r in (prior, second) if r is not None]
    return Extracted(
        triples=triples,
        drops=drops,
        parsed=second.parsed,
        raw=second.raw,
        input_tokens=sum(r.input_tokens for r in results),
        output_tokens=sum(r.output_tokens for r in results),
        cost_usd=sum(r.cost_usd for r in results),
        cache_read=sum(r.cache_read for r in results),
        attempts=2,
    )
