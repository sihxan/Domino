"""HtmlListCrawler: `spec.url#selector` → list page → matched links (absolute) → fetch_html."""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urldefrag, urljoin

import lxml.html
from cssselect import SelectorError

from domino_core.errors import PermanentError
from domino_core.models import RawDoc, SourceSpec
from domino_ingest.crawlers.base import collect, fetch_html, require_aware


def split_selector(url: str) -> tuple[str, str]:
    """Split `https://host/list#css selector` at the first `#` into (list url, selector)."""
    list_url, _, selector = url.partition("#")
    if not selector.strip():
        raise PermanentError(f"HtmlListCrawler needs a '#selector' fragment in the url: {url}")
    return list_url, selector.strip()


class HtmlListCrawler:
    """List page links selected by CSS → each link fetched with fetch_html."""

    def fetch(self, spec: SourceSpec, since: datetime) -> list[RawDoc]:
        """Fetch every linked page. A list page carries no dates, so `since` cannot filter here (idempotence is the
        caller's `url_hash` check); `published_at` stays None for the normalizer to fill from the page."""
        require_aware(since)
        list_url, selector = split_selector(spec.url)
        page, _ = fetch_html(list_url)
        try:
            root = lxml.html.fromstring(page)
            matched = root.cssselect(selector)
        except SelectorError as e:
            raise PermanentError(f"invalid css selector {selector!r}: {e}") from e
        except (lxml.etree.ParserError, ValueError) as e:
            raise PermanentError(f"list page unparsable: {list_url}") from e
        candidates: list[tuple[str, str | None, datetime | None]] = []
        seen: set[str] = set()
        for element in matched:
            for anchor in [element] if element.tag == "a" else element.iter("a"):
                href = (anchor.get("href") or "").strip()
                if not href or href.startswith("#"):
                    continue
                url, _ = urldefrag(urljoin(list_url, href))
                if not url.startswith(("http://", "https://")) or url in seen or url == list_url:
                    continue
                seen.add(url)
                title = " ".join(anchor.text_content().split()) or None
                candidates.append((url, title, None))
        return collect(spec, candidates)
