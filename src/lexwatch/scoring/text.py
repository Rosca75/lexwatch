"""Text sent to the scorer for a bill.

Choice (to record in DECISIONS.md): ``title``, then ``summary`` when a source provides one
(today none does), then an author line ``Auteur : <author>`` because the filing ministry
("Ministre des Finances", "Ministre de la Justice", ...) is a strong topical signal for short
titles. Whitespace is collapsed and the result is truncated to ``SCORING_MAX_CHARS`` (default
4000 characters, far below the rerankers' input limits). The exact text is stored in
``bills.scored_text`` so a change of this recipe is visible per bill.
"""

from __future__ import annotations

from typing import Protocol

DEFAULT_MAX_CHARS = 4000


class _BillLike(Protocol):
    title: str | None
    summary: str | None
    author: str | None


def _clean(value: str | None) -> str:
    return " ".join(value.split()) if value else ""


def bill_text(bill: _BillLike, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Title + summary + author line, whitespace-collapsed, truncated to ``max_chars``."""
    title = _clean(bill.title)
    summary = _clean(getattr(bill, "summary", None))
    author = _clean(getattr(bill, "author", None))
    parts = [p for p in (title, summary) if p]
    if summary and title and summary.casefold() == title.casefold():
        parts = [title]
    if author:
        parts.append(f"Auteur : {author}")
    text = " ".join(parts)
    if max_chars and len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return text
