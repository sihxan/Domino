"""Hedge and attribution lexicons: per-language lowercase phrase lists loaded from the YAML files in this package."""

from __future__ import annotations

from pathlib import Path

import yaml

from domino_core.errors import PermanentError

_DIR = Path(__file__).parent
REQUIRED_LANGS = ("en", "ko")


def load_lexicon(source: str | Path) -> dict[str, list[str]]:
    """Load `{lang: [phrase, ...]}` from a YAML file (a `str` names a file in this package, a `Path` is used as is).

    Raises PermanentError unless it is a mapping with at least `en` and `ko`, each a non-empty list of non-empty,
    lowercase, untrimmed strings (callers match against `quote.lower()`).
    """
    path = source if isinstance(source, Path) else _DIR / source
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise PermanentError(f"lexicon {path.name}: {e}") from e
    if not isinstance(data, dict):
        raise PermanentError(f"lexicon {path.name}: top level must be a mapping of language to phrase list")
    for lang in REQUIRED_LANGS:
        if lang not in data:
            raise PermanentError(f"lexicon {path.name}: missing language {lang!r}")
    out: dict[str, list[str]] = {}
    for lang, words in data.items():
        if not isinstance(lang, str) or not isinstance(words, list) or not words:
            raise PermanentError(f"lexicon {path.name}: {lang!r} must be a non-empty list")
        for w in words:
            if not isinstance(w, str) or not w or w != w.strip():
                raise PermanentError(f"lexicon {path.name}: {lang}: bad entry {w!r}")
            if w != w.lower():
                raise PermanentError(f"lexicon {path.name}: {lang}: entry {w!r} must be lowercase")
        out[lang] = list(words)
    return out


HEDGE = load_lexicon("hedge_lexicon.yaml")
ATTRIBUTION = load_lexicon("attribution_lexicon.yaml")
