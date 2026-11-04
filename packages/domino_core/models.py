"""Shared Pydantic models: SourceSpec, RawDoc, Document, Chunk."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class SourceSpec(BaseModel):
    id: str
    country: str
    region: Literal["NA", "EU", "CN", "EA", "ME", "EM"]
    belts: list[str] = []
    publisher: str
    grade: Literal["A", "B", "C"]
    state_controlled: bool = False
    wire: str | None = None
    kind: Literal["rss", "html", "edgar", "dart", "fred", "eia", "portwatch", "interconnection", "predmarket", "gdelt"]
    url: str
    cadence_min: int = 15
    parser: str
    lang: str
    enabled: bool = True


class RawDoc(BaseModel):
    url: str
    title: str | None
    published_at: datetime | None
    html: str
    fetched_at: datetime
    source_id: str


class Document(BaseModel):
    id: str
    source_id: str
    url: str
    url_hash: str
    title: str | None
    published_at: datetime | None
    lang: str
    doc_type: str
    text: str
    text_sha: str
    simhash: int
    snapshot_key: str
    snapshot_sha: str
    story_id: str | None = None
    wire_origin: str | None = None


class Chunk(BaseModel):
    id: str
    doc_id: str
    idx: int
    char_start: int
    char_end: int
    text: str
    n_tokens: int
