"""Crawlers: `CRAWLERS` maps `SourceSpec.parser` names to crawler classes."""

from __future__ import annotations

from domino_ingest.crawlers.base import Crawler, fetch_html
from domino_ingest.crawlers.html_list import HtmlListCrawler
from domino_ingest.crawlers.rss import RssCrawler

CRAWLERS: dict[str, type[Crawler]] = {
    "RssCrawler": RssCrawler,
    "HtmlListCrawler": HtmlListCrawler,
}

__all__ = ["CRAWLERS", "Crawler", "HtmlListCrawler", "RssCrawler", "fetch_html"]
