"""Text sent to the scorer for a bill (stream 2 may refine; record choice in DECISIONS.md)."""

from __future__ import annotations

from lexwatch.models import Bill

DEFAULT_MAX_CHARS = 4000


def bill_text(bill: Bill, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Title plus summary, whitespace-collapsed, truncated to ``max_chars``."""
    parts = [bill.title or "", bill.summary or ""]
    text = " ".join(" ".join(p.split()) for p in parts if p and p.strip())
    return text[:max_chars]
