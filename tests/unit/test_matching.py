"""Unit tests for domino_eval.matching: quote Jaccard pairing, name match, FP/FN (code section 7.1)."""

from __future__ import annotations

from typing import Any

from domino_eval.gold import GoldTriple
from domino_eval.matching import QUOTE_JACCARD_MIN, match_triples, names_match, quote_jaccard, quotes_match
from domino_extract.schema import CausalTriple


def pred(
    quote: str, cause: str = "wafer shortages", effect: str = "lead times", **kw: Any
) -> CausalTriple:
    base: dict[str, Any] = {
        "cause": {"name": cause, "type": "INFRA", "direction": "DOWN", "region": "US"},
        "effect": {"name": effect, "type": "INDICATOR", "direction": "UP", "region": "US"},
        "mechanism": "test mechanism",
        "relation_kind": "CAUSAL",
        "causal_type": "BOTTLENECK",
        "polarity": "POSITIVE",
        "horizon": "short",
        "impact": 0.5,
        "confidence": 0.8,
        "claim_type": "fact",
        "quote": quote,
    }
    return CausalTriple.model_validate(base | kw)


def gold(quote: str, cause: str = "wafer shortages", effect: str = "lead times", **kw: Any) -> GoldTriple:
    base: dict[str, Any] = {
        "cause": cause,
        "effect": effect,
        "direction_effect": "UP",
        "relation_kind": "CAUSAL",
        "causal_type": "BOTTLENECK",
        "horizon": "short",
        "claim_type": "fact",
        "quote": quote,
    }
    return GoldTriple.model_validate(base | kw)


# Character multisets: 60 x's shared, so the Jaccard is exactly 60/100 and 59/100 (whitespace is ignored).
LONG_59 = "x" * 59 + "y" * 41
SHORT_59 = "x" * 59
LONG_60 = "x" * 60 + "y" * 40
SHORT_60 = "x" * 60


# --- quote Jaccard ------------------------------------------------------------------------------


def test_threshold_constant_is_point_six() -> None:
    assert float(QUOTE_JACCARD_MIN) == 0.6


def test_quote_jaccard_059_is_not_a_match() -> None:
    assert quote_jaccard(LONG_59, SHORT_59) == 0.59
    assert not quotes_match(LONG_59, SHORT_59)


def test_quote_jaccard_060_is_a_match() -> None:
    assert quote_jaccard(LONG_60, SHORT_60) == 0.6
    assert quotes_match(LONG_60, SHORT_60)
    assert quotes_match(SHORT_60, LONG_60)  # symmetric


def test_quote_jaccard_identical_and_disjoint_and_empty() -> None:
    assert quote_jaccard("abc def", "abc def") == 1.0
    assert quote_jaccard("aaa", "bbb") == 0.0
    assert quote_jaccard("", "") == 0.0  # nothing to compare is never a match
    assert not quotes_match("", "")


def test_quote_jaccard_ignores_case_whitespace_and_nfkc() -> None:
    assert quote_jaccard("Wafer  Shortages\n push", "wafer shortages push") == 1.0
    assert quote_jaccard("\uff21\uff22\uff23", "abc") == 1.0  # NFKC full-width


def test_quote_jaccard_counts_repeated_characters() -> None:
    # multiset, not set: "aab" vs "ab" is 2/3 (a set Jaccard would call them identical)
    assert quote_jaccard("aab", "ab") == 2 / 3


# --- names --------------------------------------------------------------------------------------


def test_names_match_uses_norm_and_casefold() -> None:
    assert names_match("  U.S.  Data-Center Power Demand ", "u.s. data-center power demand")
    assert names_match("\uff27\uff24\uff30", "gdp")  # NFKC full-width
    assert names_match("Straße", "STRASSE")  # casefold, not lower
    assert not names_match("wafer shortage", "wafer shortages")


def test_cause_and_effect_are_not_interchangeable() -> None:
    res = match_triples([pred(LONG_60, cause="a", effect="b")], [gold(LONG_60, cause="b", effect="a")])
    assert res.pairs == []
    assert res.false_positives == [0] and res.false_negatives == [0]


# --- pairing ------------------------------------------------------------------------------------


def test_pairs_by_quote_overlap_and_names() -> None:
    q = "wafer shortages push lead times higher for automotive chips"
    res = match_triples([pred(q)], [gold(q)])
    assert res.pairs == [(0, 0)]
    assert res.false_positives == [] and res.false_negatives == []


def test_same_quote_but_different_name_is_no_pair() -> None:
    q = "wafer shortages push lead times higher for automotive chips"
    res = match_triples([pred(q, effect="chip prices")], [gold(q)])
    assert res.pairs == []


def test_same_names_but_quote_below_threshold_is_no_pair() -> None:
    res = match_triples([pred(LONG_59)], [gold(SHORT_59)])
    assert res.pairs == []
    assert res.false_positives == [0] and res.false_negatives == [0]


def test_unpaired_prediction_is_fp_and_unpaired_gold_is_fn() -> None:
    q1 = "wafer shortages push lead times higher"
    q2 = "rising power costs squeeze margins"
    q3 = "tariffs raise import prices"
    preds = [pred(q1), pred(q3, cause="tariffs", effect="import prices")]
    golds = [gold(q1), gold(q2, cause="power costs", effect="margins")]
    res = match_triples(preds, golds)
    assert res.pairs == [(0, 0)]
    assert res.false_positives == [1]  # predicted, not in gold
    assert res.false_negatives == [1]  # in gold, not predicted


def test_no_predictions_makes_every_gold_triple_a_fn() -> None:
    res = match_triples([], [gold(LONG_60), gold(SHORT_60, cause="c", effect="d")])
    assert res.pairs == [] and res.false_positives == [] and res.false_negatives == [0, 1]


def test_no_gold_makes_every_prediction_a_fp() -> None:
    res = match_triples([pred(LONG_60), pred(SHORT_60)], [])
    assert res.pairs == [] and res.false_positives == [0, 1] and res.false_negatives == []


def test_pairing_is_one_to_one_and_prefers_the_best_overlap() -> None:
    q_full = "wafer shortages push lead times higher for automotive chips"
    q_part = "wafer shortages push lead times higher"
    # Two predictions with the same names compete for one gold triple: the better overlap wins, the other is a FP.
    res = match_triples([pred(q_part), pred(q_full)], [gold(q_full)])
    assert res.pairs == [(1, 0)]
    assert res.false_positives == [0] and res.false_negatives == []


def test_one_to_one_leaves_a_second_gold_triple_a_fn() -> None:
    q = "wafer shortages push lead times higher for automotive chips"
    res = match_triples([pred(q)], [gold(q), gold(q)])
    assert res.pairs == [(0, 0)]  # ties go to the lower index
    assert res.false_negatives == [1]
