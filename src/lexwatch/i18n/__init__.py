"""Two small message catalogs (fr, en) with flat dotted keys.

``t(key, lang, **fmt)`` returns the translated string formatted with ``str.format``. A key
missing from the requested language falls back to French, then to the key itself (so a
missing key is visible on screen and caught by the i18n coverage test).
"""

from __future__ import annotations

import json
from decimal import Decimal
from functools import cache
from importlib import resources

LANGUAGES: tuple[str, ...] = ("fr", "en")
DEFAULT_LANGUAGE = "fr"


@cache
def catalog(lang: str) -> dict[str, str]:
    """Merged catalog for ``lang``: ``{lang}.json`` plus any ``{lang}.<part>.json``.

    Part files let streams own their keys without editing the same file (e.g. ``fr.mail.json``
    for digest strings). A key defined twice is an error.
    """
    if lang not in LANGUAGES:
        raise KeyError(lang)
    root = resources.files(__package__)
    names = sorted(
        entry.name
        for entry in root.iterdir()
        if entry.name == f"{lang}.json"
        or (entry.name.startswith(f"{lang}.") and entry.name.endswith(".json"))
    )
    merged: dict[str, str] = {}
    for name in names:
        data = json.loads(root.joinpath(name).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"i18n/{name} must be a flat JSON object")
        for key, value in data.items():
            if key in merged:
                raise ValueError(f"i18n key {key!r} defined twice ({name})")
            merged[str(key)] = str(value)
    return merged


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


SCORE_DECIMALS = 3


def format_score(value: float | Decimal | None, lang: str | None) -> str:
    """Scores and thresholds everywhere (UI, hover lines, digest): three decimals with the
    language's decimal separator (``0,425`` in French, ``0.425`` in English)."""
    if value is None:
        return ""
    text = f"{float(value):.{SCORE_DECIMALS}f}"
    return text.replace(".", ",") if normalize_lang(lang) == "fr" else text


def bill_label(number: str, bill_type: str | None, lang: str | None) -> str:
    """ "Projet de loi n° 8627" / "Government bill 8627": the bill heading, also used as the
    display title of a bill without a title."""
    type_label = t(f"bill.type.{bill_type}", lang) if bill_type else ""
    if not bill_type or type_label == f"bill.type.{bill_type}":
        type_label = t("bill.heading.default", lang)
    return t("bill.heading", lang, type=type_label, number=number)


def bill_display_title(
    title: str | None, number: str, bill_type: str | None, lang: str | None
) -> str:
    """The bill's title, or ``bill_label`` when the title is blank."""
    return title.strip() if title and title.strip() else bill_label(number, bill_type, lang)


def format_int(value: int, lang: str | None) -> str:
    """Thousands separated: ``12 039`` (no-break space) in French, ``12,039`` in English."""
    text = f"{int(value):,}"
    return text.replace(",", "\u00a0") if normalize_lang(lang) == "fr" else text
