"""Pair predicted triples with gold triples by quote overlap and cause/effect names (code section 7.1)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction

from domino_eval.gold import GoldTriple
from domino_extract.grounding import norm
from domino_extract.schema import CausalTriple

# Quotes pair when their character Jaccard is at least 0.6. Kept as an exact fraction so 0.6 itself is never lost
# to float rounding (3/5 == 60/100 exactly).
QUOTE_JACCARD_MIN = Fraction(3, 5)


@dataclass(frozen=True)
class MatchResult:
    """Pairs are `(prediction index, gold index)`; the rest are false positives / false negatives."""

    pairs: list[tuple[int, int]]
    false_positives: list[int]
    false_negatives: list[int]


def _chars(quote: str) -> Counter[str]:
    """Characters of a quote after `norm` (NFKC, one space) and casefold; whitespace itself is not counted."""
    return Counter(c for c in norm(quote).casefold() if not c.isspace())


def _jaccard(a: str, b: str) -> Fraction:
    ca, cb = _chars(a), _chars(b)
    union = sum((ca | cb).values())
    return Fraction(sum((ca & cb).values()), union) if union else Fraction(0)


def quote_jaccard(a: str, b: str) -> float:
    """Jaccard of the two quotes' character multisets (sum of min counts over sum of max counts), 0.0 if both empty."""
    return float(_jaccard(a, b))


def quotes_match(a: str, b: str) -> bool:
    """True if `quote_jaccard(a, b) >= 0.6` (exact arithmetic)."""
    return _jaccard(a, b) >= QUOTE_JACCARD_MIN


def _name_key(name: str) -> str:
    return norm(name).casefold()


def names_match(a: str, b: str) -> bool:
    """Entity names agree when `norm(x).casefold()` is equal (decision W2-13)."""
    return _name_key(a) == _name_key(b)


def match_triples(pred: Sequence[CausalTriple], gold: Sequence[GoldTriple]) -> MatchResult:
    """One-to-one pairing of the triples of one chunk.

    A candidate pair needs matching quotes, cause names and effect names (cause with cause, effect with effect).
    Pairs are taken greedily by highest quote Jaccard, ties to the lower prediction then gold index. An unpaired
    prediction is a false positive, an unpaired gold triple a false negative.
    """
    candidates: list[tuple[Fraction, int, int]] = []
    for i, p in enumerate(pred):
        for j, g in enumerate(gold):
            if not (names_match(p.cause.name, g.cause) and names_match(p.effect.name, g.effect)):
                continue
            score = _jaccard(p.quote, g.quote)
            if score >= QUOTE_JACCARD_MIN:
                candidates.append((score, i, j))
    candidates.sort(key=lambda c: (-c[0], c[1], c[2]))
    used_pred: set[int] = set()
    used_gold: set[int] = set()
    pairs: list[tuple[int, int]] = []
    for _, i, j in candidates:
        if i in used_pred or j in used_gold:
            continue
        used_pred.add(i)
        used_gold.add(j)
        pairs.append((i, j))
    pairs.sort()
    return MatchResult(
        pairs=pairs,
        false_positives=[i for i in range(len(pred)) if i not in used_pred],
        false_negatives=[j for j in range(len(gold)) if j not in used_gold],
    )
