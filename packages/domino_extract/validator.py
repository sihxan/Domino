"""Validator: ground each LLM triple in the chunk, drop self-references, demote hedged CAUSAL, relabel attributions."""

from __future__ import annotations

from typing import Literal

from domino_core.errors import PermanentError
from domino_core.models import Chunk, SourceSpec
from domino_extract.grounding import ground, norm
from domino_extract.lexicon import ATTRIBUTION, HEDGE
from domino_extract.schema import CausalTriple, ExtractionResult

# A triple dropped by `validate` and why (decision W2-8). `grounding` -> extraction_errors stage `grounding`,
# `self_ref` -> stage `validator`.
DropReason = tuple[Literal["grounding", "self_ref"], CausalTriple]

MAX_DROP_PERCENT = 30  # more than 30% of the triples dropped -> PermanentError("grounding_rate_low")


class ValidatedTriple(CausalTriple):
    """A triple that passed validation, with its chunk and its quote's span in *document* coordinates."""

    chunk_id: str
    char_start: int
    char_end: int
    is_correlation_only: bool
    dropped_reason: str | None = None  # internal, never stored


def _lexicon_phrases(lexicon: dict[str, list[str]], lang: str) -> list[str]:
    try:
        return lexicon[lang]
    except KeyError:
        # W2-10 keeps languages without a lexicon out of extraction; failing loudly beats silently skipping the
        # hedge check (which would let a speculative statement through as CAUSAL).
        raise PermanentError(f"validator: no lexicon for language {lang!r}") from None


def _contains_any(text: str, phrases: list[str]) -> bool:
    folded = norm(text).lower()
    return any(p in folded for p in phrases)


def looks_attributive(quote: str, lang: str) -> bool:
    """True if `quote` contains an attribution cue (said, announced, 밝혔다, ...) from the `lang` lexicon (W2-8)."""
    return _contains_any(quote, _lexicon_phrases(ATTRIBUTION, lang))


def validate(res: ExtractionResult, chunk: Chunk, src: SourceSpec) -> tuple[list[ValidatedTriple], list[DropReason]]:
    """Apply the code section 2.5 rules to every triple; return (kept, dropped).

    Order per triple: grounding (drop), self-reference (drop), hedge demotion CAUSAL -> SPECULATIVE, attribution for
    a `state_controlled` source. Raises PermanentError("grounding_rate_low") when more than 30% of the triples were
    dropped, so the caller can re-extract the chunk once.
    """
    hedges = _lexicon_phrases(HEDGE, src.lang)
    out: list[ValidatedTriple] = []
    drops: list[DropReason] = []
    for t in res.triples:
        span = ground(t.quote, chunk.text)
        if span is None:
            drops.append(("grounding", t))
            continue
        if norm(t.cause.name) == norm(t.effect.name):
            drops.append(("self_ref", t))
            continue
        rk = t.relation_kind
        if rk == "CAUSAL" and _contains_any(t.quote, hedges):
            rk = "SPECULATIVE"
        ct = t.claim_type
        if src.state_controlled and ct == "fact" and looks_attributive(t.quote, src.lang):
            ct = "attribution"
        out.append(
            ValidatedTriple(
                **t.model_dump(exclude={"relation_kind", "claim_type"}),
                relation_kind=rk,
                claim_type=ct,
                chunk_id=chunk.id,
                char_start=chunk.char_start + span[0],
                char_end=chunk.char_start + span[1],
                is_correlation_only=(rk != "CAUSAL"),
            )
        )
    if len(drops) * 100 > len(res.triples) * MAX_DROP_PERCENT:  # integer math: no float rounding at exactly 30%
        raise PermanentError("grounding_rate_low")
    return out, drops
