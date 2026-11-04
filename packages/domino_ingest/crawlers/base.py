"""Crawler protocol and `fetch_html`: robots.txt (RFC 9309 via protego), per-domain semaphore and request interval."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol
from urllib.parse import urlsplit

from protego import Protego

from domino_core.clients import http
from domino_core.errors import PermanentError, TransientError
from domino_core.log import get_logger
from domino_core.models import RawDoc, SourceSpec

MIN_INTERVAL_S = 2.0
ROBOTS_TTL_S = 24 * 3600.0

log = get_logger("domino_ingest.fetch")

# Given candidate URLs, the ones already stored. Set by the caller (ingest_flow) with `skip_known`, so crawlers keep
# their `fetch(spec, since)` signature and never touch the database themselves.
KnownUrls = Callable[[list[str]], set[str]]
_known_urls: ContextVar[KnownUrls | None] = ContextVar("known_urls", default=None)


@contextmanager
def skip_known(is_known: KnownUrls) -> Iterator[None]:
    """Inside the block, `collect` does not download candidates whose URL `is_known` reports as already stored."""
    token = _known_urls.set(is_known)
    try:
        yield
    finally:
        _known_urls.reset(token)


class Crawler(Protocol):
    """A source crawler: returns the documents of `spec` published since `since` (timezone-aware)."""

    def fetch(self, spec: SourceSpec, since: datetime) -> list[RawDoc]: ...


@dataclass
class Clock:
    """Injectable time source so tests can run the 2s/30s intervals without waiting."""

    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


CLOCK = Clock()


@dataclass
class _Domain:
    lock: threading.Semaphore = field(default_factory=lambda: threading.Semaphore(1))
    robots: Protego | None = None
    robots_at: float = 0.0
    last_request_end: float | None = None


_domains: dict[str, _Domain] = {}
_domains_guard = threading.Lock()


def reset_state() -> None:
    """Forget every domain's robots cache and request history (tests)."""
    with _domains_guard:
        _domains.clear()


def _domain(origin: str) -> _Domain:
    with _domains_guard:
        return _domains.setdefault(origin, _Domain())


def _origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise PermanentError(f"fetch_html: not an http(s) url: {url}")
    return f"{parts.scheme}://{parts.netloc.lower()}"


def _robots(origin: str, domain: _Domain, timeout: float) -> Protego:
    """Cached robots rules for `origin`. 2xx → parse; 4xx → no rules (allow all); 5xx/429/timeout → TransientError."""
    if domain.robots is not None and CLOCK.monotonic() - domain.robots_at < ROBOTS_TTL_S:
        return domain.robots
    try:
        _, text, _ = http.get(f"{origin}/robots.txt", timeout=timeout)
        rules = Protego.parse(text)
    except PermanentError:
        rules = Protego.parse("")
    # TransientError is not caught: an unreachable robots.txt blocks the fetch this time and is not cached.
    domain.robots = rules
    domain.robots_at = CLOCK.monotonic()
    log.info("robots_loaded", origin=origin, crawl_delay=rules.crawl_delay(http.USER_AGENT))
    return rules


def fetch_html(url: str, *, timeout: float = 20) -> tuple[str, dict[str, str]]:
    """robots.txt 확인, 도메인별 세마포어(1), max(Crawl-delay, 2초) 간격, UA 고정. 반환 (html, headers)."""
    origin = _origin(url)
    domain = _domain(origin)
    with domain.lock:
        rules = _robots(origin, domain, timeout)
        if not rules.can_fetch(url, http.USER_AGENT):
            log.info("robots_disallow", url=url)
            raise PermanentError(f"robots.txt disallows {url}")
        interval = max(rules.crawl_delay(http.USER_AGENT) or 0.0, MIN_INTERVAL_S)
        waited = 0.0
        if domain.last_request_end is not None:
            waited = max(0.0, interval - (CLOCK.monotonic() - domain.last_request_end))
            if waited > 0:
                CLOCK.sleep(waited)
        try:
            status, html, headers = http.get(url, timeout=timeout)
        except (TransientError, PermanentError) as e:
            log.info("fetch_failed", url=url, error=str(e), waited_s=round(waited, 3))
            raise
        finally:
            domain.last_request_end = CLOCK.monotonic()
        log.info("fetched", url=url, status=status, waited_s=round(waited, 3), bytes=len(html))
        return html, headers


def collect(spec: SourceSpec, candidates: Iterable[tuple[str, str | None, datetime | None]]) -> list[RawDoc]:
    """Fetch each (url, title, published_at) candidate into a RawDoc.

    A 4xx or robots-disallowed page is skipped (and logged); a transient failure aborts the source so that
    `last_ok_at` does not advance past documents that were never stored. Inside `skip_known`, candidates that are
    already stored are not downloaded at all (undated feed entries and list pages cannot be cut off by `since`).
    """
    pending = list(candidates)
    is_known = _known_urls.get()
    if is_known is not None and pending:
        known = is_known([url for url, _, _ in pending])
        if known:
            log.info("known_skipped", source_id=spec.id, n=len(known))
            pending = [c for c in pending if c[0] not in known]
    docs: list[RawDoc] = []
    for url, title, published_at in pending:
        try:
            html, _ = fetch_html(url)
        except PermanentError as e:
            log.warning("article_skipped", source_id=spec.id, url=url, error=str(e))
            continue
        docs.append(
            RawDoc(
                url=url,
                title=title,
                published_at=published_at,
                html=html,
                fetched_at=datetime.now(UTC),
                source_id=spec.id,
            )
        )
    return docs


def require_aware(since: datetime) -> None:
    """Crawlers compare against timezone-aware datetimes only (§2.3 storage convention)."""
    if since.tzinfo is None:
        raise PermanentError("since must be timezone-aware")
