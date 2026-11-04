"""Story assignment by SimHash distance and wire (reprint) detection."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from domino_core.ids import new_id
from domino_core.models import Document
from domino_core.timeutil import now_utc
from domino_ingest.simhash import hamming, shingles, simhash64

STORY_WINDOW = timedelta(hours=72)
WIRE_LEAD_CHARS = 300
WIRE_HAMMING = 3


@dataclass(frozen=True)
class StoryHead:
    """Representative (id, simhash) of a recent story, with when it last received a document."""

    id: str
    simhash: int
    last_seen: datetime


@dataclass
class WireRules:
    """Wire agency name → byline regexes, and source_id → publisher name for reprint attribution."""

    patterns: dict[str, list[re.Pattern[str]]] = field(default_factory=dict)
    publishers: dict[str, str] = field(default_factory=dict)


def assign_story(doc: Document, recent: list[StoryHead], threshold: int = 6, *, now: datetime | None = None) -> str:
    """Closest head within the 72h window and Hamming ≤ threshold → its id; otherwise a new `sty_` id."""
    cutoff = (now or now_utc()) - STORY_WINDOW
    best: StoryHead | None = None
    best_key: tuple[int, float, str] | None = None
    for head in recent:
        if head.last_seen < cutoff:
            continue
        distance = hamming(doc.simhash, head.simhash)
        if distance > threshold:
            continue
        # Closest first; ties go to the most recently seen head, then the lowest id, so the result is deterministic.
        key = (distance, -head.last_seen.timestamp(), head.id)
        if best_key is None or key < best_key:
            best, best_key = head, key
    return best.id if best else new_id("sty")


def lead_text(text: str, title: str | None) -> str:
    """Reprint comparison target: first 300 chars of `text` after a leading copy of `title` (whitespace-normalized).

    trafilatura puts the headline in front of the body, so a reprint under another headline would otherwise differ
    within the first 300 chars. Without a title, or if the text does not start with it, the text is used as is.
    """
    words = (title or "").split()
    if words:
        head = re.match(r"\s*" + r"\s+".join(map(re.escape, words)), text)
        if head:
            text = text[head.end() :].lstrip()
    return text[:WIRE_LEAD_CHARS]


def _lead_simhash(text: str, title: str | None) -> int:
    return simhash64(shingles(lead_text(text, title)))


def detect_wire(doc: Document, same_story_docs: list[Document], wires: WireRules) -> str | None:
    """Wire agency or original publisher this document reprints, or None.

    Both steps look at `lead_text`: the first 300 chars of the body after the headline.
    1) A wires.yaml byline pattern matches it → that agency.
    2) Otherwise, if another document of the story with the same lead (SimHash ≤ 3) was collected earlier
       (smaller ULID id) → that document's wire_origin, else its wire agency/publisher name.
    """
    lead = lead_text(doc.text, doc.title)
    for name, patterns in wires.patterns.items():
        if any(p.search(lead) for p in patterns):
            return name

    if not lead:
        return None
    lead_hash = _lead_simhash(doc.text, doc.title)
    earlier = [
        d
        for d in same_story_docs
        if d.id < doc.id and hamming(lead_hash, _lead_simhash(d.text, d.title)) <= WIRE_HAMMING
    ]
    if not earlier:
        return None
    origin = min(earlier, key=lambda d: d.id)
    if origin.wire_origin:
        return origin.wire_origin
    publisher = wires.publishers.get(origin.source_id, origin.source_id)
    for name in wires.patterns:
        if publisher.casefold() == name.casefold():
            return name
    return publisher
