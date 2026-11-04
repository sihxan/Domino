"""Unit tests for domino_extract.grounding: quote location with source-text (pre-normalization) offsets."""

from __future__ import annotations

import random
import re
import unicodedata

import pytest
from rapidfuzz import fuzz

from domino_extract import grounding
from domino_extract.grounding import FUZZY_MIN_SCORE, ground, norm

# Code points are written as escapes so ruff's ambiguous-unicode rules stay quiet and the fixture is unambiguous.
LDQ, RDQ, LSQ, RSQ = "\u201c", "\u201d", "\u2018", "\u2019"
BOM = "\ufeff"
NBSP = "\u00a0"
IDEOGRAPHIC_SPACE = "\u3000"


def fullwidth(s: str) -> str:
    """ASCII letters/digits/punctuation -> fullwidth forms (NFKC maps them back); space -> ideographic space."""
    return "".join(IDEOGRAPHIC_SPACE if c == " " else chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in s)


def assert_grounded(quote: str, text: str, expected: str) -> tuple[int, int]:
    """`ground` succeeds and `text[s:e]` is exactly `expected` \u2014 the source span, not a normalized one."""
    span = ground(quote, text)
    assert span is not None, f"not grounded: {quote!r} in {text!r}"
    s, e = span
    assert 0 <= s < e <= len(text)
    assert text[s:e] == expected
    return s, e


# --- norm -----------------------------------------------------------------------------------------------------------


class TestNorm:
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "  padded \t text \n\n here  ",
            fullwidth("TSMC raised capex by 20%"),
            "a" + NBSP + NBSP + "b" + IDEOGRAPHIC_SPACE + "c",
            "\ufb01nal \u2026 \u3231",  # ligature, ellipsis, parenthesized-company sign: NFKC changes the length
            "\u1112\u1161\u11ab 각 a\u0301",  # decomposed Hangul jamo, a syllable, a + combining acute
            "\x1c\x1d tab\there\r\n",
        ],
    )
    def test_matches_spec_formula(self, raw: str) -> None:
        spec = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", raw)).strip()
        assert norm(raw) == spec

    def test_whitespace_class_is_re_s(self) -> None:
        # `norm` decides whitespace with str.isspace(); the spec formula uses re's \s. They must agree everywhere.
        assert all((chr(c).isspace()) == (re.fullmatch(r"\s", chr(c)) is not None) for c in range(0x110000))

    def test_drops_bom(self) -> None:
        assert norm(BOM + "a  b" + BOM) == "a b"
        assert norm("a " + BOM + " b") == "a b"  # BOM is removed before whitespace runs collapse
        assert norm("in" + BOM + "flation") == "inflation"

    def test_idempotent(self) -> None:
        s = BOM + " " + fullwidth("Hello  World") + NBSP + "\n"
        assert norm(norm(s)) == norm(s)


# --- exact match ----------------------------------------------------------------------------------------------------


class TestExact:
    TEXT = "The central bank raised rates because inflation stayed high. Markets fell the next day."

    def test_plain_substring(self) -> None:
        quote = "because inflation stayed high"
        s, e = assert_grounded(quote, self.TEXT, quote)
        assert s == self.TEXT.index(quote)
        assert e == s + len(quote)

    def test_whole_text_and_edges(self) -> None:
        assert ground(self.TEXT, self.TEXT) == (0, len(self.TEXT))
        assert assert_grounded("The central", self.TEXT, "The central") == (0, 11)
        tail = "the next day."
        assert assert_grounded(tail, self.TEXT, tail)[1] == len(self.TEXT)

    def test_first_occurrence_wins(self) -> None:
        text = "rates rose. Then rates rose again."
        assert ground("rates rose", text) == (0, 10)

    def test_exact_beats_fuzzy(self) -> None:
        text = "supply chains broke. supply chain broke"
        assert ground("supply chain broke", text) == (21, 39)

    def test_returns_plain_ints(self) -> None:
        span = ground("central bank", self.TEXT)
        assert span is not None
        assert all(type(x) is int for x in span)


# --- normalization with offset mapping ------------------------------------------------------------------------------


class TestWhitespace:
    def test_duplicate_spaces_in_text(self) -> None:
        text = "Chip   lead  times\n\nrose\tsharply in March."
        s, e = assert_grounded("Chip lead times rose sharply", text, "Chip   lead  times\n\nrose\tsharply")
        assert s == 0
        assert e == text.index(" in March")

    def test_duplicate_spaces_in_quote(self) -> None:
        text = "Chip lead times rose sharply in March."
        assert_grounded("  lead   times\nrose  ", text, "lead times rose")

    def test_nbsp_and_ideographic_space(self) -> None:
        text = "Oil" + NBSP + "prices" + IDEOGRAPHIC_SPACE + IDEOGRAPHIC_SPACE + "fell."
        assert_grounded("Oil prices fell.", text, text)

    def test_match_never_starts_or_ends_on_whitespace(self) -> None:
        text = "alpha   beta   gamma"
        s, e = assert_grounded(" beta ", text, "beta")
        assert not text[s].isspace()
        assert not text[e - 1].isspace()


class TestNfkc:
    def test_fullwidth_text_ascii_quote(self) -> None:
        text = "News: " + fullwidth("TSMC raised capex by 20%") + " this year."
        assert_grounded("TSMC raised capex by 20%", text, fullwidth("TSMC raised capex by 20%"))

    def test_ascii_text_fullwidth_quote(self) -> None:
        text = "TSMC raised capex by 20% this year."
        assert_grounded(fullwidth("TSMC raised capex"), text, "TSMC raised capex")

    def test_length_changing_expansion(self) -> None:
        # U+FB01 (fi ligature) -> "fi", U+2026 -> "...": the normalized text is longer than the source.
        text = "The \ufb01nal rule\u2026 was delayed."
        assert_grounded("The final rule... was delayed.", text, text)
        assert_grounded("final rule", text, "\ufb01nal rule")

    def test_match_starting_inside_an_expansion_snaps_to_the_source_char(self) -> None:
        # U+3231 normalizes to "(U+682A)"; a quote that begins at ")" can only be located as the whole source char.
        text = "Issuer \u3231 Holdings reported"
        assert unicodedata.normalize("NFKC", "\u3231") == "(\u682a)"
        assert_grounded("\u682a) Holdings", text, "\u3231 Holdings")

    def test_length_changing_composition(self) -> None:
        # Decomposed Hangul (3 code points per syllable) normalizes to precomposed syllables: shorter than the source.
        nfd = unicodedata.normalize("NFD", "한국은행이 금리를 올렸다")
        text = "속보: " + nfd + "."
        assert len(text) > len(unicodedata.normalize("NFC", text))
        assert_grounded("금리를 올렸다", text, unicodedata.normalize("NFD", "금리를 올렸다"))

    def test_combining_mark_stays_with_its_base(self) -> None:
        text = "Cafe\u0301 prices rose"
        assert_grounded("Caf\u00e9 prices", text, "Cafe\u0301 prices")

    def test_char_that_decomposes_into_reordering_marks(self) -> None:
        # U+0F73 has combining class 0 but NFKC splits it into two marks that reorder with the acute before it.
        src = "a\u0301\u0f73 y"
        text = "x " + src + " z"
        assert_grounded(unicodedata.normalize("NFKC", src), text, src)

    def test_hangul_compatibility_jamo_compose_across_characters(self) -> None:
        # NFKC of two compatibility jamo is one syllable: one output char from two source chars.
        jamo = "\u314e\u314f"
        assert unicodedata.normalize("NFKC", jamo) == "하"
        assert_grounded("하 글", "x " + jamo + " 글 y", jamo + " 글")


class TestQuoteVariants:
    def test_straight_quote_vs_curly_text(self) -> None:
        text = f"The minister said {LDQ}we will not cut output{RDQ} on Tuesday."
        assert_grounded('said "we will not cut output" on', text, f"said {LDQ}we will not cut output{RDQ} on")

    def test_curly_quote_vs_straight_text(self) -> None:
        text = 'The minister said "we will not cut output" on Tuesday.'
        assert_grounded(f"said {LDQ}we will not cut output{RDQ} on", text, 'said "we will not cut output" on')

    def test_apostrophe_variants(self) -> None:
        text = f"The Fed{RSQ}s balance sheet shrank."
        assert_grounded("The Fed's balance sheet", text, f"The Fed{RSQ}s balance sheet")
        assert_grounded(f"The Fed{LSQ}s balance", "The Fed's balance sheet shrank.", "The Fed's balance")

    def test_short_quote_with_typographic_quotes_needs_no_fuzzy(self) -> None:
        # 11 chars: two substituted quote characters would score 81.8 under partial_ratio, so this only works because
        # quotes are folded before the exact search.
        text = f"He said {LDQ}yes{RDQ} today."
        quote = '"yes" today'
        assert fuzz.partial_ratio(quote, text) < FUZZY_MIN_SCORE
        assert_grounded(quote, text, f"{LDQ}yes{RDQ} today")


class TestBom:
    def test_leading_bom_in_text(self) -> None:
        text = BOM + "The central bank held rates."
        s, _ = assert_grounded("The central bank", text, "The central bank")
        assert s == 1  # the span starts at the visible character, after the BOM

    def test_bom_inside_the_match_stays_inside_the_span(self) -> None:
        text = "Core infla" + BOM + "tion eased to 2.1%."
        assert_grounded("Core inflation eased", text, "Core infla" + BOM + "tion eased")

    def test_bom_in_quote(self) -> None:
        text = "The central bank held rates."
        assert_grounded(BOM + "The central bank held" + BOM, text, "The central bank held")

    def test_bom_between_words_does_not_double_space(self) -> None:
        text = "rates" + BOM + " " + BOM + "rose sharply"
        assert_grounded("rates rose sharply", text, text)


# --- fuzzy re-search (rapidfuzz partial_ratio >= 95) ----------------------------------------------------------------


class TestFuzzy:
    PRE = "Analysts at the firm noted that "
    POST = " according to the filing."

    @staticmethod
    def _mid_substitute(s: str) -> str:
        i = len(s) // 2
        return s[:i] + ("#" if s[i] != "#" else "@") + s[i + 1 :]

    def test_threshold_constant(self) -> None:
        assert FUZZY_MIN_SCORE == 95.0

    def test_boundary_95_passes(self) -> None:
        # 20 chars, one mid substitution: indel distance 2 of 40 -> exactly 95.0.
        quote = "memory prices surged"
        assert len(quote) == 20
        window = self._mid_substitute(quote)
        text = self.PRE + window + self.POST
        assert fuzz.partial_ratio(quote, text) == 95.0
        s, e = assert_grounded(quote, text, window)
        assert s == len(self.PRE)
        assert e == s + len(window)

    def test_boundary_94_fails(self) -> None:
        # 19 chars, one mid substitution: 2 of 38 -> 94.74, just under the threshold.
        quote = "memory prices surge"
        assert len(quote) == 19
        window = self._mid_substitute(quote)
        text = self.PRE + window + self.POST
        score = fuzz.partial_ratio(quote, text)
        assert 94.0 <= score < 95.0
        assert ground(quote, text) is None

    def test_long_quote_with_a_couple_of_character_errors(self) -> None:
        quote = "the shortage of advanced packaging capacity is expected to constrain accelerator shipments"
        window = quote.replace("shortage", "shortege").replace("constrain", "constrian")
        assert window != quote
        text = self.PRE + window + self.POST
        assert fuzz.partial_ratio(quote, text) >= 95.0
        s, e = assert_grounded(quote, text, window)
        assert (s, e) == (len(self.PRE), len(self.PRE) + len(window))

    def test_fuzzy_span_is_in_source_coordinates(self) -> None:
        # Normalization (fullwidth, doubled spaces, BOM) and a one-character error together: the span still lands on
        # the source characters, not on offsets of the normalized string.
        quote = "memory prices surged as supply chains tightened across the whole region"
        damaged = quote.replace("tightened", "tightenid")
        window = fullwidth(damaged[:6]) + damaged[6:].replace(" ", "   ")
        text = "Prefix  text. " + BOM + window + "   Suffix text."
        s, e = assert_grounded(quote, text, window)
        assert text[s] == fullwidth("m")
        assert text[e - 1] == "n"

    def test_fuzzy_korean(self) -> None:
        quote = "한국은행은 반도체 수출 증가로 인해 경상수지 흑자가 크게 확대되었다고 밝혔다"
        window = quote.replace("흑자가", "흑자는")
        text = "발표문 일부: " + window + ". 이상입니다."
        assert fuzz.partial_ratio(quote, text) >= 95.0
        assert_grounded(quote, text, window)

    def test_longer_than_text_is_not_grounded(self) -> None:
        assert ground("the central bank raised rates twice in a row", "the central bank raised") is None


# --- korean ---------------------------------------------------------------------------------------------------------


class TestKorean:
    TEXT = "한국은행은 반도체 수출 증가로 인해 경상수지 흑자가 확대되었다고 밝혔다. 원화 가치는 소폭 올랐다."

    def test_exact(self) -> None:
        quote = "반도체 수출 증가로 인해 경상수지 흑자가 확대"
        s, _ = assert_grounded(quote, self.TEXT, quote)
        assert s == self.TEXT.index("반도체")

    def test_spacing_and_fullwidth_digits(self) -> None:
        text = "수출이  " + fullwidth("12") + "%\n증가했다."
        assert_grounded("수출이 12% 증가했다.", text, text)


# --- failures -------------------------------------------------------------------------------------------------------


class TestFailure:
    TEXT = "The central bank raised rates because inflation stayed high. Markets fell the next day."

    def test_absent_quote(self) -> None:
        assert ground("the ministry announced new export controls on lithium", self.TEXT) is None

    @pytest.mark.parametrize("quote", ["", " ", "\n\t ", BOM, NBSP + IDEOGRAPHIC_SPACE])
    def test_empty_after_normalization(self, quote: str) -> None:
        assert ground(quote, self.TEXT) is None

    def test_empty_text(self) -> None:
        assert ground("central bank", "") is None
        assert ground("central bank", "   ") is None

    def test_word_order_changed(self) -> None:
        assert ground("inflation stayed high because the central bank raised rates", self.TEXT) is None

    def test_short_quote_one_error_is_not_fuzzy_matched(self) -> None:
        # 12 chars with one substitution scores 91.7 \u2014 below the threshold.
        assert ground("raised rates", "The bank raised ratas today") is None


# --- invariants -----------------------------------------------------------------------------------------------------


WORDS = (
    "Chip", "lead", "times", "rose", "20%", "because", "inflation", "Fed", "oil", "prices", "supply", "chains",
    "한국은행", "금리", "수출",
)  # fmt: skip


def _render(rng: random.Random, words: list[str], *, mangle: bool) -> str:
    out = ""
    for i, w in enumerate(words):
        if mangle and rng.random() < 0.4:
            w = fullwidth(w)
        if mangle and rng.random() < 0.15:
            w = BOM + w
        out += w
        if i < len(words) - 1:
            out += rng.choice([" ", "  ", "\n", "\t ", NBSP, IDEOGRAPHIC_SPACE]) if mangle else " "
    return out


def test_random_normalization_roundtrip() -> None:
    """For any word slice of a mangled text, the span ground() returns normalizes to the quote (source coordinates)."""
    rng = random.Random(20261104)
    for _ in range(300):
        words = [rng.choice(WORDS) for _ in range(rng.randint(6, 30))]
        text = _render(rng, words, mangle=True)
        i = rng.randrange(len(words))
        j = rng.randint(i + 1, min(len(words), i + 8))
        quote = _render(rng, words[i:j], mangle=False)
        span = ground(quote, text)
        assert span is not None, (quote, text)
        s, e = span
        assert norm(text[s:e]) == norm(quote)
        assert not text[s].isspace()
        assert text[s] != BOM
        assert not text[e - 1].isspace()
        assert text[e - 1] != BOM


def test_nfkc_cluster_mismatch_falls_back_to_plain_matching(monkeypatch: pytest.MonkeyPatch) -> None:
    """If per-cluster NFKC ever disagrees with whole-string NFKC, coordinates stay exact (NFKC is skipped)."""
    monkeypatch.setattr(grounding, "_nfkc", lambda s: s.replace("abc", "Z"))
    grounding._normalize_folded.cache_clear()
    try:
        text = "x\u00a0 abc  y"
        assert ground("abc  y", text) == (3, 9)  # whitespace collapsing and exact source offsets still work ...
        assert ground("A abc", "x " + fullwidth("A") + " abc") is None  # ... but NFKC folding is skipped
    finally:
        grounding._normalize_folded.cache_clear()
