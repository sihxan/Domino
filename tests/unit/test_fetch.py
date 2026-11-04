"""fetch_html: RFC 9309 robots (protego), per-domain interval with an injected clock, error mapping (week-1 T8)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from domino_core.clients import http
from domino_core.errors import PermanentError, TransientError
from domino_ingest.crawlers import base
from domino_ingest.crawlers.base import Clock, fetch_html

Response = tuple[int, str, dict[str, str]]


def ok(text: str = "<html>ok</html>", headers: dict[str, str] | None = None) -> Response:
    return 200, text, headers or {"content-type": "text/html"}


class FakeNet:
    """Fake `clients.http.get` plus a fake clock: sleeping advances time, nothing really waits or connects."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.calls: list[tuple[float, str]] = []
        self.routes: dict[str, Response | Exception] = {}

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def get(self, url: str, *, timeout: float, headers: dict[str, str] | None = None) -> Response:
        self.calls.append((self.now, url))
        route = self.routes.get(url)
        if route is None:  # unrouted URLs behave like clients.http on a 404
            raise PermanentError(f"http 404: {url}")
        if isinstance(route, Exception):
            raise route
        return route

    def page_times(self) -> list[float]:
        return [t for t, url in self.calls if not url.endswith("/robots.txt")]

    def robots_calls(self) -> int:
        return sum(1 for _, url in self.calls if url.endswith("/robots.txt"))


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeNet]:
    fake = FakeNet()
    base.reset_state()
    monkeypatch.setattr(base, "CLOCK", Clock(monotonic=fake.monotonic, sleep=fake.sleep))
    monkeypatch.setattr(http, "get", fake.get)
    yield fake
    base.reset_state()


BOK_ROBOTS = "User-agent: *\nDisallow: /\nAllow: /portal/\n"


# --- robots decisions ----------------------------------------------------------------------


def test_returns_html_and_headers(net: FakeNet) -> None:
    net.routes["https://a.example/p"] = ok("<html>hello</html>", {"content-type": "text/html", "etag": "x"})
    html, headers = fetch_html("https://a.example/p")
    assert html == "<html>hello</html>"
    assert headers == {"content-type": "text/html", "etag": "x"}


def test_robots_disallow_raises_permanent_without_requesting_the_page(net: FakeNet) -> None:
    net.routes["https://a.example/robots.txt"] = ok("User-agent: *\nDisallow: /private/\n")
    net.routes["https://a.example/private/x"] = ok()
    with pytest.raises(PermanentError, match="robots"):
        fetch_html("https://a.example/private/x")
    assert [url for _, url in net.calls] == ["https://a.example/robots.txt"]


def test_bank_of_korea_style_robots_allows_portal_longest_match(net: FakeNet) -> None:
    net.routes["https://bok.example/robots.txt"] = ok(BOK_ROBOTS)
    net.routes["https://bok.example/portal/bbs/B0000245/view.do"] = ok("portal")
    net.routes["https://bok.example/other/page"] = ok("other")
    assert fetch_html("https://bok.example/portal/bbs/B0000245/view.do")[0] == "portal"
    with pytest.raises(PermanentError, match="robots"):
        fetch_html("https://bok.example/other/page")


def test_longest_match_beats_rule_order(net: FakeNet) -> None:
    net.routes["https://a.example/robots.txt"] = ok("User-agent: *\nAllow: /news/\nDisallow: /news/draft\n")
    net.routes["https://a.example/news/draft/1"] = ok()
    net.routes["https://a.example/news/1"] = ok("news")
    with pytest.raises(PermanentError, match="robots"):
        fetch_html("https://a.example/news/draft/1")
    assert fetch_html("https://a.example/news/1")[0] == "news"


def test_group_for_our_user_agent_token_is_used(net: FakeNet) -> None:
    net.routes["https://a.example/robots.txt"] = ok("User-agent: *\nDisallow:\n\nUser-agent: DominoBot\nDisallow: /\n")
    net.routes["https://a.example/p"] = ok()
    assert http.USER_AGENT.startswith("DominoBot")
    with pytest.raises(PermanentError, match="robots"):
        fetch_html("https://a.example/p")


def test_group_for_another_bot_is_ignored(net: FakeNet) -> None:
    net.routes["https://a.example/robots.txt"] = ok("User-agent: OtherBot\nDisallow: /\n")
    net.routes["https://a.example/p"] = ok("fine")
    assert fetch_html("https://a.example/p")[0] == "fine"


def test_robots_404_allows_everything(net: FakeNet) -> None:
    net.routes["https://a.example/any/path"] = ok("fine")  # robots.txt unrouted → 404
    assert fetch_html("https://a.example/any/path")[0] == "fine"


def test_robots_403_counts_as_unavailable_and_allows(net: FakeNet) -> None:
    net.routes["https://a.example/robots.txt"] = PermanentError("http 403: https://a.example/robots.txt")
    net.routes["https://a.example/p"] = ok("fine")
    assert fetch_html("https://a.example/p")[0] == "fine"


@pytest.mark.parametrize("failure", [TransientError("http 503: robots"), TransientError("http ReadTimeout: robots")])
def test_robots_unreachable_blocks_with_transient_and_is_not_cached(net: FakeNet, failure: TransientError) -> None:
    net.routes["https://a.example/robots.txt"] = failure
    net.routes["https://a.example/p"] = ok("fine")
    with pytest.raises(TransientError):
        fetch_html("https://a.example/p")
    assert net.page_times() == []
    net.routes["https://a.example/robots.txt"] = ok("User-agent: *\nDisallow:\n")  # server recovers
    assert fetch_html("https://a.example/p")[0] == "fine"
    assert net.robots_calls() == 2


def test_robots_cached_per_domain_for_a_day(net: FakeNet) -> None:
    net.routes["https://a.example/robots.txt"] = ok("User-agent: *\nDisallow:\n")
    net.routes["https://a.example/1"] = ok()
    net.routes["https://a.example/2"] = ok()
    fetch_html("https://a.example/1")
    fetch_html("https://a.example/2")
    assert net.robots_calls() == 1
    net.now += 24 * 3600 + 1
    fetch_html("https://a.example/1")
    assert net.robots_calls() == 2


def test_robots_cached_separately_per_origin(net: FakeNet) -> None:
    net.routes["https://a.example/robots.txt"] = ok("User-agent: *\nDisallow: /\n")
    net.routes["https://b.example/p"] = ok("fine")
    net.routes["https://a.example/p"] = ok()
    with pytest.raises(PermanentError, match="robots"):
        fetch_html("https://a.example/p")
    assert fetch_html("https://b.example/p")[0] == "fine"


# --- interval ----------------------------------------------------------------------


def test_same_domain_without_crawl_delay_waits_two_seconds(net: FakeNet) -> None:
    net.routes["https://a.example/1"] = ok()
    net.routes["https://a.example/2"] = ok()
    net.routes["https://a.example/3"] = ok()
    for n in (1, 2, 3):
        fetch_html(f"https://a.example/{n}")
    t = net.page_times()
    assert t[1] - t[0] >= 2 and t[2] - t[1] >= 2
    assert net.sleeps == [2.0, 2.0]


def test_crawl_delay_30_waits_thirty_seconds(net: FakeNet) -> None:
    net.routes["https://de.example/robots.txt"] = ok("User-agent: *\nCrawl-delay: 30\nDisallow: /intern/\n")
    net.routes["https://de.example/1"] = ok()
    net.routes["https://de.example/2"] = ok()
    fetch_html("https://de.example/1")
    fetch_html("https://de.example/2")
    t = net.page_times()
    assert t[1] - t[0] >= 30


def test_crawl_delay_below_two_seconds_still_waits_two(net: FakeNet) -> None:
    net.routes["https://a.example/robots.txt"] = ok("User-agent: *\nCrawl-delay: 1\n")
    net.routes["https://a.example/1"] = ok()
    net.routes["https://a.example/2"] = ok()
    fetch_html("https://a.example/1")
    fetch_html("https://a.example/2")
    t = net.page_times()
    assert t[1] - t[0] >= 2


def test_different_domains_do_not_wait(net: FakeNet) -> None:
    net.routes["https://a.example/p"] = ok()
    net.routes["https://b.example/p"] = ok()
    fetch_html("https://a.example/p")
    fetch_html("https://b.example/p")
    assert net.sleeps == []


def test_elapsed_time_counts_toward_the_interval(net: FakeNet) -> None:
    net.routes["https://a.example/1"] = ok()
    net.routes["https://a.example/2"] = ok()
    fetch_html("https://a.example/1")
    net.now += 5
    fetch_html("https://a.example/2")
    assert net.sleeps == []


def test_first_request_to_a_domain_does_not_wait_for_robots_fetch(net: FakeNet) -> None:
    net.routes["https://a.example/robots.txt"] = ok("User-agent: *\nCrawl-delay: 30\n")
    net.routes["https://a.example/1"] = ok()
    fetch_html("https://a.example/1")
    assert net.sleeps == []


def test_failed_request_still_counts_toward_the_interval(net: FakeNet) -> None:
    net.routes["https://a.example/1"] = TransientError("http 503")
    net.routes["https://a.example/2"] = ok()
    with pytest.raises(TransientError):
        fetch_html("https://a.example/1")
    fetch_html("https://a.example/2")
    t = net.page_times()
    assert t[1] - t[0] >= 2


# --- status mapping ----------------------------------------------------------------------


def test_page_4xx_is_permanent(net: FakeNet) -> None:
    net.routes["https://a.example/p"] = PermanentError("http 404: https://a.example/p")
    with pytest.raises(PermanentError):
        fetch_html("https://a.example/p")


@pytest.mark.parametrize("failure", [TransientError("http 503: p"), TransientError("http ReadTimeout: p")])
def test_page_5xx_and_timeout_are_transient(net: FakeNet, failure: TransientError) -> None:
    net.routes["https://a.example/p"] = failure
    with pytest.raises(TransientError):
        fetch_html("https://a.example/p")


def test_timeout_is_passed_through_to_the_client(net: FakeNet, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[float] = []

    def get(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> Response:
        seen.append(timeout)
        return net.get(url, timeout=timeout, headers=headers)

    monkeypatch.setattr(http, "get", get)
    net.routes["https://a.example/p"] = ok()
    fetch_html("https://a.example/p", timeout=7)
    assert seen == [7, 7]  # robots.txt then the page
