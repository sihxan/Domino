"""Source registry: load and validate `sources/*.yaml`, load `wires.yaml`, upsert into the `sources` table."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from sqlalchemy import Engine, text

from domino_core.clients.db import connection
from domino_core.errors import PermanentError
from domino_core.models import SourceSpec
from domino_ingest.crawlers import CRAWLERS

# Crawler class names `SourceSpec.parser` may use: the keys of `crawlers.CRAWLERS`.
CRAWLER_NAMES = frozenset(CRAWLERS)

WIRES_FILE = "wires.yaml"

_ID = re.compile(r"src_[a-z0-9_]+")
_COUNTRY = re.compile(r"[A-Z]{2}")
_LANG = re.compile(r"[a-z]{2}")

# Every `sources` column except the operational state (last_ok_at, last_error, fail_count): upsert never touches those.
_UPSERT = text(
    """
    INSERT INTO sources
      (id, country, region, belts, publisher, grade, state_controlled, wire, kind, url, cadence_min, parser, lang,
       enabled)
    VALUES
      (:id, :country, :region, CAST(:belts AS text[]), :publisher, CAST(:grade AS source_grade), :state_controlled,
       :wire, :kind, :url, :cadence_min, :parser, :lang, :enabled)
    ON CONFLICT (id) DO UPDATE SET
      country = EXCLUDED.country,
      region = EXCLUDED.region,
      belts = EXCLUDED.belts,
      publisher = EXCLUDED.publisher,
      grade = EXCLUDED.grade,
      state_controlled = EXCLUDED.state_controlled,
      wire = EXCLUDED.wire,
      kind = EXCLUDED.kind,
      url = EXCLUDED.url,
      cadence_min = EXCLUDED.cadence_min,
      parser = EXCLUDED.parser,
      lang = EXCLUDED.lang,
      enabled = EXCLUDED.enabled
    """
)


def _read_yaml(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise PermanentError(f"{path.name}: cannot read yaml ({type(e).__name__})") from e


def _validate(spec: SourceSpec, where: str) -> None:
    """Rules beyond the pydantic model: id/country/lang shape, registered parser, url, cadence."""
    if not _ID.fullmatch(spec.id):
        raise PermanentError(f"{where}: id must match src_[a-z0-9_]+: {spec.id!r}")
    if not _COUNTRY.fullmatch(spec.country):
        raise PermanentError(f"{where}: country must be ISO-3166 alpha-2 uppercase: {spec.country!r}")
    if not _LANG.fullmatch(spec.lang):
        raise PermanentError(f"{where}: lang must be two lowercase letters: {spec.lang!r}")
    if spec.parser not in CRAWLER_NAMES:
        raise PermanentError(f"{where}: parser {spec.parser!r} is not a registered crawler {sorted(CRAWLER_NAMES)}")
    if not spec.url.startswith(("http://", "https://")):
        raise PermanentError(f"{where}: url must be http(s): {spec.url!r}")
    if spec.cadence_min <= 0:
        raise PermanentError(f"{where}: cadence_min must be positive: {spec.cadence_min}")
    if spec.parser == "HtmlListCrawler" and not spec.url.partition("#")[2]:
        raise PermanentError(f"{where}: HtmlListCrawler url needs a '#selector' fragment: {spec.url!r}")


def load_sources(directory: Path) -> list[SourceSpec]:
    """Validate every `<dir>/*.yaml` except `wires.yaml` into `SourceSpec`s; duplicate ids are rejected."""
    files = sorted(p for p in directory.glob("*.yaml") if p.name != WIRES_FILE)
    if not files:
        raise PermanentError(f"no source yaml files in {directory}")
    specs: list[SourceSpec] = []
    seen: dict[str, str] = {}
    for path in files:
        data = _read_yaml(path)
        if not isinstance(data, list):
            raise PermanentError(f"{path.name}: top level must be a list of sources")
        for i, raw in enumerate(data):
            where = f"{path.name}[{i}]"
            try:
                spec = SourceSpec.model_validate(raw)
            except ValidationError as e:
                raise PermanentError(f"{where}: {e}") from e
            _validate(spec, where)
            if spec.id in seen:
                raise PermanentError(f"{where}: duplicate source id {spec.id!r} (first in {seen[spec.id]})")
            seen[spec.id] = where
            specs.append(spec)
    return specs


def load_wires(path: Path) -> dict[str, list[re.Pattern[str]]]:
    """Read `wires.yaml` (agency name → byline regexes) and compile every pattern."""
    data = _read_yaml(path)
    if not isinstance(data, dict):
        raise PermanentError(f"{path.name}: top level must be a mapping of agency name to regex list")
    wires: dict[str, list[re.Pattern[str]]] = {}
    for name, patterns in data.items():
        if not isinstance(patterns, list) or not all(isinstance(p, str) for p in patterns):
            raise PermanentError(f"{path.name}: {name}: must be a list of regex strings")
        compiled: list[re.Pattern[str]] = []
        for p in patterns:
            try:
                compiled.append(re.compile(p))
            except re.error as e:
                raise PermanentError(f"{path.name}: {name}: bad regex {p!r}: {e}") from e
        wires[str(name)] = compiled
    return wires


def upsert_sources(specs: list[SourceSpec], *, engine: Engine | None = None) -> int:
    """Upsert specs into `sources` by id in one transaction; last_ok_at/last_error/fail_count are left alone."""
    if not specs:
        return 0
    rows = [s.model_dump() for s in specs]
    with connection(engine=engine) as conn:
        conn.execute(_UPSERT, rows)
    return len(rows)


def main() -> None:
    """`make seed` entry point: load `sources/` from the working directory and upsert."""
    directory = Path("sources")
    load_wires(directory / WIRES_FILE)
    n = upsert_sources(load_sources(directory))
    print(f"sources: {n} upserted")


if __name__ == "__main__":
    main()
