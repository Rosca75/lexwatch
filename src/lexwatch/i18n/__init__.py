"""Two small message catalogs (fr, en) with flat dotted keys.

``t(key, lang, **fmt)`` returns the translated string formatted with ``str.format``. A key
missing from the requested language falls back to French, then to the key itself (so a
missing key is visible on screen and caught by the i18n coverage test).
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources

LANGUAGES: tuple[str, ...] = ("fr", "en")
DEFAULT_LANGUAGE = "fr"


@lru_cache(maxsize=None)
def catalog(lang: str) -> dict[str, str]:
    if lang not in LANGUAGES:
        raise KeyError(lang)
    text = resources.files(__package__).joinpath(f"{lang}.json").read_text(encoding="utf-8")
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError(f"i18n/{lang}.json must be a flat JSON object")
    return {str(k): str(v) for k, v in data.items()}


def load_catalogs() -> None:
    """Load (and validate) all catalogs; call once at startup so errors surface early."""
    for lang in LANGUAGES:
        catalog(lang)


def normalize_lang(lang: str | None) -> str:
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def t(key: str, lang: str | None = None, **fmt: object) -> str:
    lang = normalize_lang(lang)
    text = catalog(lang).get(key)
    if text is None:
        text = catalog(DEFAULT_LANGUAGE).get(key, key)
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError, ValueError):
            return text
    return text
