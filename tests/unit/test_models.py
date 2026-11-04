"""domino_core.models: shared Pydantic models."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from domino_core.models import SourceSpec

VALID: dict[str, Any] = {
    "id": "src_us_fed_press",
    "country": "US",
    "region": "NA",
    "publisher": "Federal Reserve",
    "grade": "A",
    "kind": "rss",
    "url": "https://www.federalreserve.gov/feeds/press_all.xml",
    "parser": "rss",
    "lang": "en",
}


def test_valid_source_spec_defaults() -> None:
    spec = SourceSpec(**VALID)
    assert spec.belts == []
    assert spec.state_controlled is False
    assert spec.wire is None
    assert spec.cadence_min == 15
    assert spec.enabled is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("region", "US"),
        ("region", "na"),
        ("grade", "D"),
        ("grade", "a"),
        ("kind", "atom"),
        ("kind", "RSS"),
    ],
)
def test_source_spec_rejects_invalid_enum(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        SourceSpec(**{**VALID, field: value})


@pytest.mark.parametrize("region", ["NA", "EU", "CN", "EA", "ME", "EM"])
def test_source_spec_accepts_all_regions(region: str) -> None:
    assert SourceSpec(**{**VALID, "region": region}).region == region


@pytest.mark.parametrize(
    "kind", ["rss", "html", "edgar", "dart", "fred", "eia", "portwatch", "interconnection", "predmarket", "gdelt"]
)
def test_source_spec_accepts_all_kinds(kind: str) -> None:
    assert SourceSpec(**{**VALID, "kind": kind}).kind == kind
