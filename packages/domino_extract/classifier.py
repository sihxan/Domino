"""Pre-classification: keyword prefilter, then a one-letter yes/no LLM call (code section 2.3)."""

from __future__ import annotations

from domino_core.clients import default_settings, llm
from domino_core.errors import BudgetExceeded, PermanentError, TransientError, UsageLimitReached
from domino_core.log import get_logger
from domino_core.models import Chunk
from domino_core.timeutil import report_date
from domino_extract.lexicon import HEDGE

log = get_logger(__name__)

CAUSAL_HINTS: dict[str, list[str]] = {
    "en": [
        "because", "due to", "driven by", "leads to", "led to", "as a result", "impact", "effect",
        "shortage", "lead time", "pushes", "forces", "caus",
    ],
    "ko": ["때문", "영향", "따라", "결과", "이어", "초래", "부족", "리드타임", "압박", "밀어", "끌어"],
}  # fmt: skip

# Languages the pipeline extracts from (W2-10): those with both a hint list and a hedge lexicon.
EXTRACT_LANGS: tuple[str, ...] = tuple(sorted(set(CAUSAL_HINTS) & set(HEDGE)))

CLASSIFY_SYSTEM = (
    "Answer Y if the text asserts or reports that one thing causes, drives, enables, constrains or replaces "
    "another (any language). Otherwise N. Answer with one letter."
)


def classify(chunk: Chunk, lang: str) -> bool:
    """Whether `chunk` may contain a causal statement and should go to extraction.

    No hint word -> False without an LLM call. Otherwise ask the model; a transient or permanent LLM failure passes
    the chunk on (conservative, tech section 5). `BudgetExceeded` and `UsageLimitReached` propagate: the run must stop
    its LLM stage, and passing the chunk on would only send it to an extract call that fails the same way.
    """
    try:
        hints = CAUSAL_HINTS[lang]
    except KeyError:
        raise PermanentError(f"classifier: no causal hints for language {lang!r}") from None
    if not any(h in chunk.text.lower() for h in hints):
        return False
    try:
        r = llm.call(
            task="classify",
            model=default_settings().model_extract,
            system=CLASSIFY_SYSTEM,
            user=chunk.text,
            max_tokens=2,
            budget_key=f"budget:classify:{report_date().isoformat()}",
        )
    except (BudgetExceeded, UsageLimitReached):
        raise
    except (TransientError, PermanentError) as e:
        log.warning("classify_failed_passing_chunk", chunk_id=chunk.id, error=type(e).__name__, detail=str(e)[:200])
        return True
    return r.raw.strip().upper().startswith("Y")
