"""Raw HTML → Document: immutable snapshot first, trafilatura text, language, doc_type, SimHash."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

import trafilatura
from langdetect import DetectorFactory, LangDetectException, detect

from domino_core.clients.s3 import put_immutable
from domino_core.errors import PermanentError
from domino_core.ids import new_id
from domino_core.log import get_logger
from domino_core.models import Document, RawDoc, SourceSpec
from domino_ingest.simhash import shingles, simhash64

MIN_TEXT_CHARS = 300
SIMHASH_CHARS = 2000

DetectorFactory.seed = 0  # langdetect is randomized otherwise; same text must give the same language

log = get_logger(__name__)

_DEFAULT_PORTS = {"http": 80, "https": 443}


class SkippedDocError(PermanentError):
    """Not normalizable, but the snapshot is already stored: carries what `insert_skipped` needs."""

    reason = "skipped"

    def __init__(self, doc_id: str, snapshot_key: str, snapshot_sha: str) -> None:
        self.doc_id = doc_id
        self.snapshot_key = snapshot_key
        self.snapshot_sha = snapshot_sha
        super().__init__(self.reason)


class TooShortError(SkippedDocError):
    """Extracted text is under 300 characters (`status='skipped'`)."""

    reason = "too_short"


class LangUndetectedError(SkippedDocError):
    """Source gave no language and langdetect could not find one."""

    reason = "lang_undetected"


def normalize_url(url: str) -> str:
    """Conservative canonical URL: lowercase scheme and host, no fragment, no default port, empty path → '/'."""
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = host
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"
    if parts.username is not None:
        userinfo = parts.username if parts.password is None else f"{parts.username}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))


def url_hash(url: str) -> str:
    """sha256 hex of the normalized URL (`raw_documents.url_hash`)."""
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def doc_type_for(spec: SourceSpec) -> str:
    """edgar/dart → FILING; rss/html grade-A non-wire source → RELEASE; everything else NEWS."""
    if spec.kind in ("edgar", "dart"):
        return "FILING"
    if spec.kind in ("rss", "html") and spec.grade == "A" and spec.wire is None:
        return "RELEASE"
    return "NEWS"


def _sha256(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _page_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _detect_lang(text: str) -> str | None:
    try:
        return str(detect(text))[:2]
    except LangDetectException:
        return None


def normalize(raw: RawDoc, spec: SourceSpec) -> Document:
    """Store the snapshot, extract text and metadata, and build the `Document`.

    Raises `TooShortError` (a `PermanentError`) when the text is under 300 characters; it carries the doc_id and
    snapshot so the caller records a skipped row without storing the snapshot again.
    """
    if raw.fetched_at.tzinfo is None:
        raise PermanentError(f"fetched_at must be timezone-aware: {raw.url}")
    fetched = raw.fetched_at.astimezone(UTC)

    doc_id = new_id("doc")
    snapshot_key = f"raw/{fetched:%Y}/{fetched:%m}/{doc_id}.html"
    body = raw.html.encode("utf-8")
    snapshot_sha = hashlib.sha256(body).hexdigest()
    put_immutable(snapshot_key, body)

    text = (
        (trafilatura.extract(raw.html, include_comments=False, favor_precision=True) or "").strip() if raw.html else ""
    )
    if len(text) < MIN_TEXT_CHARS:
        log.info("normalize_skipped", reason="too_short", doc_id=doc_id, source_id=spec.id, n_chars=len(text))
        raise TooShortError(doc_id, snapshot_key, snapshot_sha)

    meta = trafilatura.extract_metadata(raw.html)
    published_at = raw.published_at or _page_date(meta.date if meta else None)
    title = raw.title or (meta.title if meta else None)

    lang = spec.lang or _detect_lang(text)
    if not lang:
        log.info("normalize_skipped", reason="lang_undetected", doc_id=doc_id, source_id=spec.id)
        raise LangUndetectedError(doc_id, snapshot_key, snapshot_sha)

    return Document(
        id=doc_id,
        source_id=spec.id,
        url=raw.url,
        url_hash=url_hash(raw.url),
        title=title,
        published_at=published_at,
        lang=lang,
        doc_type=doc_type_for(spec),
        text=text,
        text_sha=_sha256(text),
        simhash=simhash64(shingles(text[:SIMHASH_CHARS], n=3)),
        snapshot_key=snapshot_key,
        snapshot_sha=snapshot_sha,
    )
