"""RssCrawler and HtmlListCrawler against fixtures; no network (`clients.http.get` is faked) (week-1 T8)."""

from __future__ import annotations

import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from domino_core.clients import http
from domino_core.errors import PermanentError, TransientError
from domino_core.models import SourceSpec
from domino_ingest.crawlers import CRAWLERS, base
from domino_ingest.crawlers.base import Clock
from domino_ingest.crawlers.html_list import HtmlListCrawler
from domino_ingest.crawlers.rss import RssCrawler
from domino_ingest.sources import CRAWLER_NAMES

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "ingest"
FEED = (FIXTURES / "feed.xml").read_text(encoding="utf-8")
LIST_HTML = (FIXTURES / "list.html").read_text(encoding="utf-8")
ARTICLE = (FIXTURES / "article.html").read_text(encoding="utf-8")

FEED_URL = "https://www.example.org/press/feed.xml"
LIST_URL = "https://www.example.org/press/index.html"
SINCE = datetime(2026, 11, 1, tzinfo=UTC)

Response = tuple[int, str, dict[str, str]]


class FakeNet:
    """Fake `clients.http.get` plus a fake clock (sleeping advances time, nothing really waits)."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.calls: list[str] = []
        self.routes: dict[str, Response | Exception] = {}

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def get(self, url: str, *, timeout: float, headers: dict[str, str] | None = None) -> Response:
        self.calls.append(url)
        route = self.routes.get(url)
        if route is None:
            raise PermanentError(f"http 404: {url}")
        if isinstance(route, Exception):
            raise route
        return route

    def page(self, url: str, text: str) -> None:
        self.routes[url] = (200, text, {"content-type": "text/html"})

    def requested(self) -> list[str]:
        return [u for u in self.calls if not u.endswith("/robots.txt")]


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeNet]:
    fake = FakeNet()
    base.reset_state()
    monkeypatch.setattr(base, "CLOCK", Clock(monotonic=fake.monotonic, sleep=fake.sleep))
    monkeypatch.setattr(http, "get", fake.get)
    yield fake
    base.reset_state()


def spec(parser: str, url: str, kind: str = "rss") -> SourceSpec:
    return SourceSpec.model_validate(
        {
            "id": "src_xx_test",
            "country": "XX",
            "region": "EU",
            "publisher": "Example Agency",
            "grade": "A",
            "kind": kind,
            "url": url,
            "parser": parser,
            "lang": "en",
        }
    )


def rss_net(net: FakeNet, feed: str = FEED) -> None:
    net.page(FEED_URL, feed)
    for name in ("a", "b", "c-old", "d-undated"):
        net.page(f"https://www.example.org/news/{name}", ARTICLE)


# --- registry ----------------------------------------------------------------------


def test_crawlers_registry_maps_names_to_classes() -> None:
    assert {"RssCrawler": RssCrawler, "HtmlListCrawler": HtmlListCrawler} == CRAWLERS
    assert frozenset(CRAWLERS) == CRAWLER_NAMES


# --- RssCrawler ----------------------------------------------------------------------


def test_rss_returns_only_entries_since_with_absolute_links(net: FakeNet) -> None:
    rss_net(net)
    docs = RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE)
    assert [d.url for d in docs] == [
        "https://www.example.org/news/a",
        "https://www.example.org/news/b",  # relative link resolved against the feed URL
        "https://www.example.org/news/d-undated",
    ]
    assert "https://www.example.org/news/c-old" not in net.requested()  # old entries are not even fetched
    assert net.requested()[0] == FEED_URL


def test_rss_rawdoc_fields(net: FakeNet) -> None:
    rss_net(net)
    a, b, d = RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE)
    assert a.title == "Industrial production rose 0.4% in September"
    assert a.published_at == datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
    assert b.published_at == datetime(2026, 11, 2, 8, 30, tzinfo=UTC)
    assert a.html == ARTICLE
    assert a.source_id == "src_xx_test"
    assert a.fetched_at.tzinfo is not None
    assert d.published_at is None  # undated entries are kept; the normalizer reads the date from the page


def test_rss_since_boundary_is_inclusive(net: FakeNet) -> None:
    rss_net(net)
    docs = RssCrawler().fetch(spec("RssCrawler", FEED_URL), datetime(2026, 11, 3, 10, 0, tzinfo=UTC))
    assert [d.url for d in docs] == ["https://www.example.org/news/a", "https://www.example.org/news/d-undated"]


def test_rss_old_since_returns_every_entry_that_has_a_link(net: FakeNet) -> None:
    rss_net(net)
    docs = RssCrawler().fetch(spec("RssCrawler", FEED_URL), datetime(2020, 1, 1, tzinfo=UTC))
    assert len(docs) == 4  # the entry without a link is dropped


def test_rss_naive_since_is_rejected(net: FakeNet) -> None:
    rss_net(net)
    with pytest.raises(PermanentError):
        RssCrawler().fetch(spec("RssCrawler", FEED_URL), datetime(2026, 11, 1))
    assert net.calls == []


def test_rss_article_4xx_is_skipped_and_the_rest_kept(net: FakeNet) -> None:
    rss_net(net)
    del net.routes["https://www.example.org/news/a"]  # 404
    docs = RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE)
    assert [d.url for d in docs] == ["https://www.example.org/news/b", "https://www.example.org/news/d-undated"]


def test_rss_article_robots_disallow_is_skipped(net: FakeNet) -> None:
    rss_net(net)
    net.page("https://www.example.org/robots.txt", "User-agent: *\nDisallow: /news/b\n")
    docs = RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE)
    assert [d.url for d in docs] == ["https://www.example.org/news/a", "https://www.example.org/news/d-undated"]


def test_rss_article_transient_failure_propagates(net: FakeNet) -> None:
    rss_net(net)
    net.routes["https://www.example.org/news/b"] = TransientError("http 503")
    with pytest.raises(TransientError):
        RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE)


def test_rss_feed_failures_propagate(net: FakeNet) -> None:
    with pytest.raises(PermanentError):
        RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE)  # feed 404
    net.routes[FEED_URL] = TransientError("http 503")
    with pytest.raises(TransientError):
        RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE)


def test_rss_unparsable_feed_is_permanent(net: FakeNet) -> None:
    net.page(FEED_URL, "<html><body>Service temporarily unavailable</body></html>")
    with pytest.raises(PermanentError, match="feed"):
        RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE)


def test_rss_empty_but_valid_feed_returns_nothing(net: FakeNet) -> None:
    net.page(FEED_URL, '<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>')
    assert RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE) == []


def test_rss_feedparser_never_touches_the_network(net: FakeNet, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("feedparser opened a URL itself")

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", boom)
    net.page(FEED_URL, "http://127.0.0.1:9/feed.xml")  # a body that looks like a URL must be parsed as data
    with pytest.raises(PermanentError, match="feed"):
        RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE)


def test_rss_declared_encoding_does_not_garble_decoded_text(net: FakeNet) -> None:
    feed = (
        '<?xml version="1.0" encoding="ISO-8859-1"?><rss version="2.0"><channel><title>t</title>'
        "<item><title>Preisentwicklung für Güter</title><link>https://www.example.org/news/a</link>"
        "<pubDate>Tue, 03 Nov 2026 10:00:00 GMT</pubDate></item></channel></rss>"
    )
    net.page(FEED_URL, feed)  # already decoded to str by the HTTP layer
    net.page("https://www.example.org/news/a", ARTICLE)
    (doc,) = RssCrawler().fetch(spec("RssCrawler", FEED_URL), SINCE)
    assert doc.title == "Preisentwicklung für Güter"


# --- HtmlListCrawler ----------------------------------------------------------------------


def list_net(net: FakeNet) -> None:
    net.page(LIST_URL, LIST_HTML)
    for url in (
        "https://www.example.org/press/2026/a.html",
        "https://www.example.org/press/2026/b.html",
        "https://www.example.org/archive/c.html",
        "https://other.example.net/d",
        "https://www.example.org/press/2026/e.html",
    ):
        net.page(url, ARTICLE)


def test_html_list_splits_selector_and_makes_links_absolute(net: FakeNet) -> None:
    list_net(net)
    docs = HtmlListCrawler().fetch(spec("HtmlListCrawler", f"{LIST_URL}#ul.news-list a", "html"), SINCE)
    assert [d.url for d in docs] == [
        "https://www.example.org/press/2026/a.html",
        "https://www.example.org/press/2026/b.html",
        "https://www.example.org/archive/c.html",  # `../` resolved
        "https://other.example.net/d",
        "https://www.example.org/press/2026/e.html",  # fragment dropped, duplicate of /a.html collapsed
    ]
    assert net.requested()[0] == LIST_URL  # the selector fragment is not sent


def test_html_list_rawdoc_fields(net: FakeNet) -> None:
    list_net(net)
    docs = HtmlListCrawler().fetch(spec("HtmlListCrawler", f"{LIST_URL}#ul.news-list a", "html"), SINCE)
    assert docs[0].title == "Statement on the October labour market"
    assert docs[2].title == "Archived note on reserve statistics"  # whitespace collapsed
    assert docs[0].published_at is None
    assert docs[0].html == ARTICLE
    assert docs[0].source_id == "src_xx_test"
    assert docs[0].fetched_at.tzinfo is not None


def test_html_list_selector_may_match_a_container(net: FakeNet) -> None:
    list_net(net)
    docs = HtmlListCrawler().fetch(spec("HtmlListCrawler", f"{LIST_URL}#ul.news-list", "html"), SINCE)
    assert len(docs) == 5


def test_html_list_ignores_links_outside_the_selector(net: FakeNet) -> None:
    list_net(net)
    net.page("https://www.example.org/about.html", ARTICLE)
    net.page("https://www.example.org/contact.html", ARTICLE)
    docs = HtmlListCrawler().fetch(spec("HtmlListCrawler", f"{LIST_URL}#nav a", "html"), SINCE)
    assert [d.url for d in docs] == ["https://www.example.org/about.html", "https://www.example.org/contact.html"]
    docs = HtmlListCrawler().fetch(spec("HtmlListCrawler", f"{LIST_URL}#ul.news-list a", "html"), SINCE)
    assert all("about" not in d.url and "contact" not in d.url for d in docs)


def test_html_list_selector_with_hash_inside_uses_the_first_hash_as_separator(net: FakeNet) -> None:
    list_net(net)
    docs = HtmlListCrawler().fetch(spec("HtmlListCrawler", f"{LIST_URL}##content ul a", "html"), SINCE)
    assert len(docs) == 5


def test_html_list_missing_or_invalid_selector_is_permanent(net: FakeNet) -> None:
    list_net(net)
    with pytest.raises(PermanentError, match="selector"):
        HtmlListCrawler().fetch(spec("HtmlListCrawler", LIST_URL, "html"), SINCE)
    with pytest.raises(PermanentError, match="selector"):
        HtmlListCrawler().fetch(spec("HtmlListCrawler", f"{LIST_URL}#", "html"), SINCE)
    with pytest.raises(PermanentError, match="selector"):
        HtmlListCrawler().fetch(spec("HtmlListCrawler", f"{LIST_URL}#a[", "html"), SINCE)


def test_html_list_article_4xx_skipped_transient_propagates(net: FakeNet) -> None:
    list_net(net)
    del net.routes["https://www.example.org/press/2026/b.html"]
    docs = HtmlListCrawler().fetch(spec("HtmlListCrawler", f"{LIST_URL}#ul.news-list a", "html"), SINCE)
    assert len(docs) == 4
    net.routes["https://other.example.net/d"] = TransientError("http 503")
    with pytest.raises(TransientError):
        HtmlListCrawler().fetch(spec("HtmlListCrawler", f"{LIST_URL}#ul.news-list a", "html"), SINCE)
