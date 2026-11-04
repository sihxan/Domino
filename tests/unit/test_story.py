"""assign_story (72h window, threshold) and detect_wire (byline regex, text[:300] hamming)."""

from __future__ import annotations

import random
import re
from datetime import UTC, datetime, timedelta

import pytest

from domino_core.models import Document
from domino_ingest.simhash import hamming, shingles, simhash64
from domino_ingest.story import StoryHead, WireRules, assign_story, detect_wire, lead_text

NOW = datetime(2026, 11, 4, 0, 0, tzinfo=UTC)

_VOCAB = [
    "port",
    "container",
    "throughput",
    "harbor",
    "vessel",
    "berth",
    "freight",
    "dwell",
    "crane",
    "customs",
    "backlog",
    "rate",
    "shipping",
    "terminal",
    "cargo",
    "schedule",
    "delay",
    "lane",
    "canal",
    "draft",
    "tonnage",
]


def _words(n: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    return [rng.choice(_VOCAB) for _ in range(n)]


def _sim300(text: str) -> int:
    return simhash64(shingles(text[:300]))


def make_doc(
    doc_id: str, text: str, *, source_id: str = "src_us_a", simhash: int = 0, title: str | None = None
) -> Document:
    return Document(
        id=doc_id,
        source_id=source_id,
        url=f"https://example.com/{doc_id}",
        url_hash="0" * 64,
        title=title,
        published_at=None,
        lang="en",
        doc_type="NEWS",
        text=text,
        text_sha="0" * 64,
        simhash=simhash,
        snapshot_key=f"raw/2026/11/{doc_id}.html",
        snapshot_sha="0" * 64,
    )


def flip(value: int, n_bits: int) -> int:
    """`value` with exactly `n_bits` low bits flipped (Hamming distance n_bits)."""
    return value ^ ((1 << n_bits) - 1)


# --- assign_story -------------------------------------------------------------------------------


def test_assign_story_joins_head_at_threshold_6() -> None:
    doc = make_doc("doc_1", "x", simhash=0xABCDEF0123456789)
    head = StoryHead(id="sty_a", simhash=flip(doc.simhash, 6), last_seen=NOW - timedelta(hours=1))
    assert hamming(doc.simhash, head.simhash) == 6
    assert assign_story(doc, [head], now=NOW) == "sty_a"


def test_assign_story_starts_new_story_at_7() -> None:
    doc = make_doc("doc_1", "x", simhash=0xABCDEF0123456789)
    head = StoryHead(id="sty_a", simhash=flip(doc.simhash, 7), last_seen=NOW - timedelta(hours=1))
    story_id = assign_story(doc, [head], now=NOW)
    assert story_id != "sty_a"
    assert re.fullmatch(r"sty_[0-9A-HJKMNP-TV-Z]{26}", story_id)


def test_assign_story_threshold_is_a_parameter() -> None:
    doc = make_doc("doc_1", "x", simhash=0)
    head = StoryHead(id="sty_a", simhash=flip(0, 9), last_seen=NOW)
    assert assign_story(doc, [head], threshold=9, now=NOW) == "sty_a"
    assert assign_story(doc, [head], threshold=8, now=NOW) != "sty_a"


def test_assign_story_ignores_heads_outside_72h_window() -> None:
    doc = make_doc("doc_1", "x", simhash=123)
    old = StoryHead(id="sty_old", simhash=123, last_seen=NOW - timedelta(hours=72, seconds=1))
    edge = StoryHead(id="sty_edge", simhash=flip(123, 3), last_seen=NOW - timedelta(hours=72))
    assert assign_story(doc, [old], now=NOW) != "sty_old"
    assert assign_story(doc, [old, edge], now=NOW) == "sty_edge"


def test_assign_story_picks_closest_head() -> None:
    doc = make_doc("doc_1", "x", simhash=0)
    far = StoryHead(id="sty_far", simhash=flip(0, 5), last_seen=NOW)
    near = StoryHead(id="sty_near", simhash=flip(0, 2), last_seen=NOW)
    assert assign_story(doc, [far, near], now=NOW) == "sty_near"
    assert assign_story(doc, [near, far], now=NOW) == "sty_near"


def test_assign_story_empty_recent_makes_new_story() -> None:
    assert assign_story(make_doc("doc_1", "x"), [], now=NOW).startswith("sty_")


# --- detect_wire --------------------------------------------------------------------------------


@pytest.fixture
def wires() -> WireRules:
    return WireRules(
        patterns={
            "reuters": [re.compile(r"^\(Reuters\)"), re.compile(r"Reporting by .* Editing by")],
            "yonhap": [re.compile(r"\(서울=연합뉴스\)")],
        },
        publishers={
            "src_us_a": "Federal Reserve",
            "src_us_wire": "Reuters",
            "src_kr_b": "Bank of Korea",
        },
    )


LEAD = " ".join(_words(60, seed=11))[:300]


def test_detect_wire_matches_byline_in_first_300_chars(wires: WireRules) -> None:
    doc = make_doc("doc_2", "(Reuters) " + " ".join(_words(80, seed=12)))
    assert detect_wire(doc, [], wires) == "reuters"


def test_detect_wire_matches_korean_byline(wires: WireRules) -> None:
    doc = make_doc("doc_2", "(서울=연합뉴스) 한국은행은 기준금리를 동결했다. " + "본문 " * 100)
    assert detect_wire(doc, [], wires) == "yonhap"


def test_detect_wire_ignores_byline_after_first_300_chars(wires: WireRules) -> None:
    text = " ".join(_words(80, seed=13))
    assert len(text[:300]) == 300
    doc = make_doc("doc_2", text + " (Reuters) " + "tail " * 10)
    assert detect_wire(doc, [], wires) is None


def test_detect_wire_returns_none_for_original_report(wires: WireRules) -> None:
    doc = make_doc("doc_2", LEAD + " " + " ".join(_words(80, seed=14)))
    assert detect_wire(doc, [], wires) is None


def test_detect_wire_reprint_of_wire_uses_wire_name(wires: WireRules) -> None:
    first = make_doc("doc_1", LEAD + " alpha " * 40, source_id="src_us_wire")
    later = make_doc("doc_2", LEAD + " beta " * 40, source_id="src_kr_b")
    assert detect_wire(later, [first, later], wires) == "reuters"


def test_detect_wire_reprint_of_non_wire_uses_that_publisher(wires: WireRules) -> None:
    first = make_doc("doc_1", LEAD + " alpha " * 40, source_id="src_us_a")
    later = make_doc("doc_2", LEAD + " beta " * 40, source_id="src_kr_b")
    assert detect_wire(later, [first, later], wires) == "Federal Reserve"


def test_detect_wire_first_collected_document_is_not_a_reprint(wires: WireRules) -> None:
    first = make_doc("doc_1", LEAD + " alpha " * 40, source_id="src_us_a")
    later = make_doc("doc_2", LEAD + " beta " * 40, source_id="src_kr_b")
    assert detect_wire(first, [first, later], wires) is None


def test_detect_wire_uses_earliest_matching_document(wires: WireRules) -> None:
    a = make_doc("doc_1", LEAD + " alpha " * 40, source_id="src_us_a")
    b = make_doc("doc_2", LEAD + " beta " * 40, source_id="src_us_wire")
    c = make_doc("doc_3", LEAD + " gamma " * 40, source_id="src_kr_b")
    assert detect_wire(c, [b, a, c], wires) == "Federal Reserve"


def test_detect_wire_earlier_doc_with_wire_origin_keeps_it(wires: WireRules) -> None:
    first = make_doc("doc_1", LEAD + " alpha " * 40, source_id="src_us_a")
    first.wire_origin = "reuters"
    later = make_doc("doc_2", LEAD + " beta " * 40, source_id="src_kr_b")
    assert detect_wire(later, [first, later], wires) == "reuters"


def _variant_at_distance(base: str, distance: int) -> str:
    """A copy of `base` with a few words swapped so the text[:300] simhash differs by exactly `distance` bits."""
    words = base.split()
    rng = random.Random(distance)
    for _ in range(5000):
        cand = list(words)
        for i in rng.sample(range(len(cand)), k=rng.randint(1, 6)):
            cand[i] = rng.choice(_VOCAB)
        text = " ".join(cand)
        if hamming(_sim300(base), _sim300(text)) == distance:
            return text
    raise AssertionError(f"no variant at distance {distance}")  # pragma: no cover


def test_detect_wire_text300_hamming_boundary_3_vs_4(wires: WireRules) -> None:
    base = " ".join(_words(70, seed=21))
    first = make_doc("doc_1", base, source_id="src_us_a")
    at3 = make_doc("doc_2", _variant_at_distance(base, 3), source_id="src_kr_b")
    at4 = make_doc("doc_3", _variant_at_distance(base, 4), source_id="src_kr_b")
    assert hamming(_sim300(first.text), _sim300(at3.text)) == 3
    assert hamming(_sim300(first.text), _sim300(at4.text)) == 4
    assert detect_wire(at3, [first, at3], wires) == "Federal Reserve"
    assert detect_wire(at4, [first, at4], wires) is None


# --- title-prefixed reprints (decisions:104, week-2 T5) -------------------------------------------

TITLE_A = "Port backlog eases as container throughput recovers at major harbors"
TITLE_B = "Harbor delays shrink while cargo schedule improves across terminals"
BODY = " ".join(_words(120, seed=5))


def test_title_prefixed_copy_detected(wires: WireRules) -> None:
    """trafilatura puts the title in front of the body, so a reprint with another headline used to miss (hamming 14)."""
    first = make_doc("doc_1", f"{TITLE_A}\n\n{BODY}", source_id="src_us_a", title=TITLE_A)
    later = make_doc("doc_2", f"{TITLE_B}\n\n{BODY}", source_id="src_kr_b", title=TITLE_B)
    assert hamming(_sim300(first.text), _sim300(later.text)) > 3  # the old comparison target really does miss
    assert detect_wire(later, [first, later], wires) == "Federal Reserve"
    assert detect_wire(first, [first, later], wires) is None


def test_title_prefix_matches_after_whitespace_normalization(wires: WireRules) -> None:
    first = make_doc("doc_1", f"{TITLE_A}\n\n{BODY}", source_id="src_us_a", title=TITLE_A)
    spaced = TITLE_B.replace(" ", "  ", 3).replace("Harbor delays", "Harbor\ndelays")
    later = make_doc("doc_2", f"\n{spaced}\n\n{BODY}", source_id="src_kr_b", title=TITLE_B)
    assert detect_wire(later, [first, later], wires) == "Federal Reserve"


def test_byline_is_matched_after_title_is_stripped(wires: WireRules) -> None:
    """`^\\(Reuters\\)` is anchored at the start of the body, which the title used to occupy."""
    doc = make_doc("doc_2", f"{TITLE_A}\n\n(Reuters) - {BODY}", title=TITLE_A)
    assert detect_wire(doc, [], wires) == "reuters"


def test_byline_inside_the_title_does_not_count(wires: WireRules) -> None:
    title = "(Reuters) - Port backlog eases"
    doc = make_doc("doc_2", f"{title}\n\n{BODY}", title=title)
    assert detect_wire(doc, [], wires) is None


def test_text_not_starting_with_title_is_compared_whole(wires: WireRules) -> None:
    first = make_doc("doc_1", BODY, source_id="src_us_a", title=TITLE_A)
    later = make_doc("doc_2", BODY, source_id="src_kr_b", title=TITLE_B)
    assert detect_wire(later, [first, later], wires) == "Federal Reserve"
    assert lead_text(BODY, TITLE_A) == BODY[:300]


def test_lead_text_strips_title_prefix_and_takes_300_chars() -> None:
    assert lead_text(f"{TITLE_A}\n\n{BODY}", TITLE_A) == BODY[:300]
    # the prefix match is exact apart from whitespace: another case is not the title
    assert lead_text(f"  {TITLE_A.upper()}\n{BODY}", TITLE_A) != BODY[:300]
    assert lead_text(f"{TITLE_A}\n\n{BODY}", None) == f"{TITLE_A}\n\n{BODY}"[:300]
    assert lead_text(f"{TITLE_A}\n\n{BODY}", "  ") == f"{TITLE_A}\n\n{BODY}"[:300]
    assert lead_text(TITLE_A, TITLE_A) == ""  # a body that is only the title leaves nothing to compare


def test_empty_lead_is_never_a_reprint(wires: WireRules) -> None:
    first = make_doc("doc_1", TITLE_A, source_id="src_us_a", title=TITLE_A)
    later = make_doc("doc_2", TITLE_B, source_id="src_kr_b", title=TITLE_B)
    assert detect_wire(later, [first, later], wires) is None
