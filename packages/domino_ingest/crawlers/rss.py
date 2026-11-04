"""RssCrawler: feed via fetch_html, parsed with feedparser (no feedparser networking), entries since `since`."""

from __future__ import annotations

import io
from calendar import timegm
from datetime import UTC, datetime
from urllib.parse import urljoin

import feedparser

from domino_core.errors import PermanentError
from domino_core.models import RawDoc, SourceSpec
from domino_ingest.crawlers.base import collect, fetch_html, require_aware


def _published(entry: feedparser.FeedParserDict) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime.fromtimestamp(timegm(parsed), UTC) if parsed else None


class RssCrawler:
    """feedparser → each entry.link fetched with fetch_html."""

    def fetch(self, spec: SourceSpec, since: datetime) -> list[RawDoc]:
        """Return a RawDoc per feed entry published at or after `since`; undated entries are kept."""
        require_aware(since)
        feed_text, _ = fetch_html(spec.url)
        # A stream (not a str) so feedparser never treats the body as a URL or file name; the header pins the charset
        # to the utf-8 we encode with, whatever the XML declaration says (the HTTP layer already decoded the text).
        parsed = feedparser.parse(
            io.BytesIO(feed_text.encode("utf-8")),
            response_headers={"content-type": "application/xml; charset=utf-8"},
        )
        if not parsed.version or (parsed.bozo and not parsed.entries):
            raise PermanentError(f"feed unparsable: {spec.url}")
        candidates: list[tuple[str, str | None, datetime | None]] = []
        seen: set[str] = set()
        for entry in parsed.entries:
            link = (entry.get("link") or "").strip()
            if not link:
                continue
            url = urljoin(spec.url, link)
            published_at = _published(entry)
            if url in seen or (published_at is not None and published_at < since):
                continue
            seen.add(url)
            title = (entry.get("title") or "").strip() or None
            candidates.append((url, title, published_at))
        return collect(spec, candidates)
