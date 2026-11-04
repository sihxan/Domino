"""normalize: snapshot first, trafilatura text, too_short, lang, doc_type, simhash; URL normalization."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from domino_core.errors import PermanentError
from domino_core.models import RawDoc, SourceSpec
from domino_ingest import normalizer
from domino_ingest.normalizer import TooShortError, doc_type_for, normalize, normalize_url, url_hash
from domino_ingest.simhash import shingles, simhash64

FETCHED = datetime(2026, 11, 4, 3, 30, tzinfo=UTC)

BODY_EN = (
    "The central bank left its policy rate unchanged on Tuesday and said inflation continued to ease "
    "toward its target. Officials noted that wage growth had slowed for a third straight quarter, while "
    "energy prices remained volatile because of supply disruptions in several producing regions. "
    "The statement added that the committee would review incoming data before its next meeting."
)
BODY_KO = (
    "한국은행 금융통화위원회는 오늘 기준금리를 현 수준에서 유지하기로 결정했다. 물가 상승률이 점차 "
    "둔화되고 있으나 에너지 가격의 변동성이 여전히 크다는 점을 위원회는 지적했다. 국내 성장세는 "
    "수출 호조에 힘입어 완만한 회복 흐름을 이어가고 있으며, 위원회는 향후 발표될 지표를 점검해 "
    "다음 회의에서 정책 방향을 다시 논의할 예정이라고 밝혔다. 한편 시장에서는 환율과 가계부채 흐름이 "
    "통화정책 경로에 미칠 영향에 관심이 쏠리고 있으며, 위원회는 대외 여건의 불확실성이 높은 만큼 "
    "물가와 성장 사이의 균형을 면밀히 살피겠다고 덧붙였다."
)


def page(body: str, *, title: str = "Rates unchanged", date: str | None = "2026-11-03", heading: bool = True) -> str:
    meta = f'<meta property="article:published_time" content="{date}">' if date else ""
    h1 = f"<h1>{title}</h1>" if heading else ""
    return f"<html><head><title>{title}</title>{meta}</head><body><article>{h1}<p>{body}</p></article></body></html>"


def spec(**kw: Any) -> SourceSpec:
    base: dict[str, Any] = {
        "id": "src_us_fed_press",
        "country": "US",
        "region": "NA",
        "publisher": "Federal Reserve",
        "grade": "A",
        "kind": "rss",
        "url": "https://www.federalreserve.gov/feeds/press_all.xml",
        "parser": "RssCrawler",
        "lang": "en",
    }
    base.update(kw)
    return SourceSpec(**base)


def raw(html: str, *, published_at: datetime | None = None, title: str | None = "Feed title") -> RawDoc:
    return RawDoc(
        url="https://www.federalreserve.gov/newsevents/pressreleases/monetary20261103a.htm",
        title=title,
        published_at=published_at,
        html=html,
        fetched_at=FETCHED,
        source_id="src_us_fed_press",
    )


@pytest.fixture
def puts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes]]:
    calls: list[tuple[str, bytes]] = []

    def fake_put(key: str, body: bytes, **_: Any) -> bool:
        calls.append((key, body))
        return True

    monkeypatch.setattr(normalizer, "put_immutable", fake_put)
    return calls


# --- happy path ---------------------------------------------------------------------------------


def test_normalize_builds_document_and_stores_snapshot_once(puts: list[tuple[str, bytes]]) -> None:
    html = page(BODY_EN)
    doc = normalize(raw(html), spec())

    assert re.fullmatch(r"doc_[0-9A-HJKMNP-TV-Z]{26}", doc.id)
    assert doc.snapshot_key == f"raw/2026/11/{doc.id}.html"
    assert doc.snapshot_sha == hashlib.sha256(html.encode()).hexdigest()
    assert puts == [(doc.snapshot_key, html.encode())]

    assert BODY_EN[:60] in doc.text
    assert len(doc.text) >= 300
    assert doc.text_sha == hashlib.sha256(doc.text.encode()).hexdigest()
    assert doc.source_id == "src_us_fed_press"
    assert doc.url_hash == url_hash(doc.url)
    assert doc.simhash == simhash64(shingles(doc.text[:2000], n=3))
    assert doc.story_id is None and doc.wire_origin is None


def test_snapshot_key_uses_fetched_at_year_and_month(puts: list[tuple[str, bytes]]) -> None:
    r = raw(page(BODY_EN)).model_copy(update={"fetched_at": datetime(2027, 1, 31, 23, 59, tzinfo=UTC)})
    doc = normalize(r, spec())
    assert doc.snapshot_key == f"raw/2027/01/{doc.id}.html"


def test_naive_fetched_at_rejected_before_any_write(puts: list[tuple[str, bytes]]) -> None:
    r = raw(page(BODY_EN)).model_copy(update={"fetched_at": datetime(2026, 11, 4, 3, 30)})
    with pytest.raises(PermanentError):
        normalize(r, spec())
    assert puts == []


# --- published_at / title -----------------------------------------------------------------------


def test_published_at_prefers_raw_over_page_metadata(puts: list[tuple[str, bytes]]) -> None:
    feed_date = datetime(2026, 11, 3, 18, 0, tzinfo=UTC)
    doc = normalize(raw(page(BODY_EN, date="2026-11-01"), published_at=feed_date), spec())
    assert doc.published_at == feed_date


def test_published_at_falls_back_to_page_metadata_as_utc(puts: list[tuple[str, bytes]]) -> None:
    doc = normalize(raw(page(BODY_EN, date="2026-11-03")), spec())
    assert doc.published_at == datetime(2026, 11, 3, tzinfo=UTC)


def test_published_at_none_when_neither_source_has_it(puts: list[tuple[str, bytes]]) -> None:
    doc = normalize(raw(page(BODY_EN, date=None)), spec())
    assert doc.published_at is None


def test_title_prefers_raw_then_page(puts: list[tuple[str, bytes]]) -> None:
    assert normalize(raw(page(BODY_EN)), spec()).title == "Feed title"
    assert normalize(raw(page(BODY_EN, title="Page title"), title=None), spec()).title == "Page title"


# --- too_short ----------------------------------------------------------------------------------


def test_too_short_raises_with_doc_and_snapshot_after_one_put(puts: list[tuple[str, bytes]]) -> None:
    html = page("Rates unchanged.")
    with pytest.raises(TooShortError) as exc:
        normalize(raw(html), spec())

    e = exc.value
    assert isinstance(e, PermanentError)
    assert str(e) == "too_short"
    assert re.fullmatch(r"doc_[0-9A-HJKMNP-TV-Z]{26}", e.doc_id)
    assert e.snapshot_key == f"raw/2026/11/{e.doc_id}.html"
    assert e.snapshot_sha == hashlib.sha256(html.encode()).hexdigest()
    assert puts == [(e.snapshot_key, html.encode())]


def test_exactly_300_chars_is_accepted_and_299_is_not(puts: list[tuple[str, bytes]]) -> None:
    varied = "".join(f"{w} " for w in BODY_EN.split())
    body300 = varied[:299].rstrip() + "."
    assert len(body300) == 300
    assert len(normalize(raw(page(body300, heading=False)), spec()).text) == 300
    with pytest.raises(TooShortError):
        normalize(raw(page(body300[:-1], heading=False)), spec())


def test_empty_html_is_too_short_not_a_crash(puts: list[tuple[str, bytes]]) -> None:
    with pytest.raises(TooShortError):
        normalize(raw(""), spec())
    assert len(puts) == 1


# --- lang ---------------------------------------------------------------------------------------


def test_lang_prefers_spec(puts: list[tuple[str, bytes]]) -> None:
    # English text, but the source says Korean: spec wins (mixed-language documents).
    assert normalize(raw(page(BODY_EN)), spec(lang="ko")).lang == "ko"


def test_lang_detected_when_spec_has_none(puts: list[tuple[str, bytes]]) -> None:
    assert normalize(raw(page(BODY_KO)), spec(lang="")).lang == "ko"
    assert normalize(raw(page(BODY_EN)), spec(lang="")).lang == "en"


# --- doc_type -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "expected"),
    [
        ({"kind": "rss", "grade": "A"}, "RELEASE"),
        ({"kind": "html", "grade": "A", "parser": "HtmlListCrawler"}, "RELEASE"),
        ({"kind": "rss", "grade": "A", "wire": "reuters"}, "NEWS"),
        ({"kind": "rss", "grade": "B"}, "NEWS"),
        ({"kind": "html", "grade": "C", "parser": "HtmlListCrawler"}, "NEWS"),
        ({"kind": "edgar", "grade": "A", "parser": "EdgarFullTextCrawler"}, "FILING"),
        ({"kind": "dart", "grade": "A", "parser": "DartCrawler"}, "FILING"),
        ({"kind": "gdelt", "grade": "A", "parser": "GdeltDiscoveryCrawler"}, "NEWS"),
    ],
)
def test_doc_type_mapping(kw: dict[str, Any], expected: str) -> None:
    assert doc_type_for(spec(**kw)) == expected


def test_normalize_sets_doc_type(puts: list[tuple[str, bytes]]) -> None:
    assert normalize(raw(page(BODY_EN)), spec()).doc_type == "RELEASE"
    assert normalize(raw(page(BODY_EN)), spec(grade="B")).doc_type == "NEWS"


# --- URL normalization --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("HTTPS://WWW.Example.COM/Path/Page?Q=1", "https://www.example.com/Path/Page?Q=1"),
        ("https://example.com/a#section", "https://example.com/a"),
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("http://example.com", "http://example.com/"),
        ("https://example.com/a?b=2&a=1", "https://example.com/a?b=2&a=1"),
        ("  https://example.com/a  ", "https://example.com/a"),
    ],
)
def test_normalize_url(given: str, expected: str) -> None:
    assert normalize_url(given) == expected


def test_url_hash_is_sha256_of_normalized_url() -> None:
    assert url_hash("HTTPS://Example.com:443/a#x") == hashlib.sha256(b"https://example.com/a").hexdigest()
    assert url_hash("https://example.com/a") == url_hash("https://EXAMPLE.com/a#frag")
    assert url_hash("https://example.com/a") != url_hash("https://example.com/b")
