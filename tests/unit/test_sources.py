"""Source registry loader: repo YAML validation, duplicate ids, parser names, wire regexes (week-1 T7)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from domino_core.errors import PermanentError
from domino_ingest.sources import CRAWLER_NAMES, load_sources, load_wires

ROOT = Path(__file__).resolve().parents[2]
SOURCES_DIR = ROOT / "sources"
COUNTRIES = ["us", "kr", "jp", "tw", "de"]

VALID = {
    "id": "src_us_test",
    "country": "US",
    "region": "NA",
    "belts": ["power"],
    "publisher": "Test Agency",
    "grade": "A",
    "kind": "rss",
    "url": "https://example.com/feed.xml",
    "cadence_min": 30,
    "parser": "RssCrawler",
    "lang": "en",
}


def write_sources(tmp_path: Path, name: str, entries: list[dict[str, object]]) -> Path:
    (tmp_path / f"{name}.yaml").write_text(yaml.safe_dump(entries, allow_unicode=True), encoding="utf-8")
    return tmp_path


def entry(**overrides: object) -> dict[str, object]:
    return {**VALID, **overrides}


# --- repository YAML -------------------------------------------------------------------------------------------------


def test_repo_sources_load_fifteen_across_five_countries() -> None:
    specs = load_sources(SOURCES_DIR)
    assert len(specs) == 15
    assert {s.country for s in specs} == {c.upper() for c in COUNTRIES}
    for cc in COUNTRIES:
        assert sum(1 for s in specs if s.country == cc.upper()) == 3


def test_repo_sources_are_rss_or_html_with_registered_parsers() -> None:
    for s in load_sources(SOURCES_DIR):
        assert s.kind in {"rss", "html"}
        assert s.parser in CRAWLER_NAMES
        assert s.url.startswith("https://")


def test_repo_source_ids_unique_and_human_keys() -> None:
    ids = [s.id for s in load_sources(SOURCES_DIR)]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"src_[a-z0-9_]+", i) for i in ids)


def test_repo_wires_compile() -> None:
    wires = load_wires(SOURCES_DIR / "wires.yaml")
    assert {"reuters", "ap", "afp", "yonhap", "kyodo", "cna", "dpa", "xinhua"} <= set(wires)
    for patterns in wires.values():
        assert patterns
        assert all(isinstance(p, re.Pattern) for p in patterns)
    assert any(p.search("(서울=연합뉴스) 홍길동 기자") for p in wires["yonhap"])
    assert any(p.search("(Reuters) - Oil prices rose") for p in wires["reuters"])


# --- load_sources validation -----------------------------------------------------------------------------------------


def test_load_sources_returns_specs_from_all_yaml_files_except_wires(tmp_path: Path) -> None:
    write_sources(tmp_path, "us", [entry()])
    write_sources(tmp_path, "kr", [entry(id="src_kr_test", country="KR", region="EA", lang="ko")])
    (tmp_path / "wires.yaml").write_text("reuters: ['^\\(Reuters\\)']\n", encoding="utf-8")
    specs = load_sources(tmp_path)
    assert sorted(s.id for s in specs) == ["src_kr_test", "src_us_test"]


def test_duplicate_id_in_one_file_rejected(tmp_path: Path) -> None:
    write_sources(tmp_path, "us", [entry(), entry()])
    with pytest.raises(PermanentError, match="duplicate"):
        load_sources(tmp_path)


def test_duplicate_id_across_files_rejected(tmp_path: Path) -> None:
    write_sources(tmp_path, "us", [entry()])
    write_sources(tmp_path, "kr", [entry(country="KR", region="EA")])
    with pytest.raises(PermanentError, match="duplicate"):
        load_sources(tmp_path)


def test_unregistered_parser_rejected(tmp_path: Path) -> None:
    write_sources(tmp_path, "us", [entry(parser="EdgarFullTextCrawler")])
    with pytest.raises(PermanentError, match="parser"):
        load_sources(tmp_path)


@pytest.mark.parametrize(
    "bad",
    [
        {"region": "XX"},
        {"grade": "D"},
        {"kind": "carrier-pigeon"},
        {"country": "USA"},
        {"country": "us"},
        {"lang": "eng"},
        {"id": "fed_press"},
        {"url": "ftp://example.com/feed.xml"},
        {"cadence_min": 0},
    ],
)
def test_invalid_field_rejected_as_permanent_error(tmp_path: Path, bad: dict[str, object]) -> None:
    write_sources(tmp_path, "us", [entry(**bad)])
    with pytest.raises(PermanentError):
        load_sources(tmp_path)


def test_missing_required_field_rejected(tmp_path: Path) -> None:
    broken = entry()
    del broken["publisher"]
    write_sources(tmp_path, "us", [broken])
    with pytest.raises(PermanentError):
        load_sources(tmp_path)


def test_html_parser_requires_selector_fragment(tmp_path: Path) -> None:
    write_sources(tmp_path, "us", [entry(kind="html", parser="HtmlListCrawler", url="https://example.com/news")])
    with pytest.raises(PermanentError, match="selector"):
        load_sources(tmp_path)
    ok = entry(kind="html", parser="HtmlListCrawler", url="https://example.com/news#ul.list a")
    write_sources(tmp_path, "us", [ok])
    assert load_sources(tmp_path)[0].url == "https://example.com/news#ul.list a"


def test_file_that_is_not_a_list_rejected(tmp_path: Path) -> None:
    (tmp_path / "us.yaml").write_text("id: src_us_test\n", encoding="utf-8")
    with pytest.raises(PermanentError):
        load_sources(tmp_path)


def test_invalid_yaml_rejected(tmp_path: Path) -> None:
    (tmp_path / "us.yaml").write_text("- id: [unclosed\n", encoding="utf-8")
    with pytest.raises(PermanentError):
        load_sources(tmp_path)


def test_empty_directory_rejected(tmp_path: Path) -> None:
    with pytest.raises(PermanentError):
        load_sources(tmp_path)


# --- wires -----------------------------------------------------------------------------------------------------------


def test_load_wires_compiles_patterns(tmp_path: Path) -> None:
    path = tmp_path / "wires.yaml"
    path.write_text("reuters: ['^\\(Reuters\\)', 'Reporting by .* Editing by']\n", encoding="utf-8")
    wires = load_wires(path)
    assert list(wires) == ["reuters"]
    assert wires["reuters"][0].search("(Reuters) - hello")


def test_load_wires_rejects_bad_regex(tmp_path: Path) -> None:
    path = tmp_path / "wires.yaml"
    path.write_text("reuters: ['(unclosed']\n", encoding="utf-8")
    with pytest.raises(PermanentError, match="reuters"):
        load_wires(path)


def test_load_wires_rejects_non_mapping_or_non_list(tmp_path: Path) -> None:
    path = tmp_path / "wires.yaml"
    path.write_text("- reuters\n", encoding="utf-8")
    with pytest.raises(PermanentError):
        load_wires(path)
    path.write_text("reuters: '^x'\n", encoding="utf-8")
    with pytest.raises(PermanentError):
        load_wires(path)
