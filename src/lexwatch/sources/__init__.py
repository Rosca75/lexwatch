"""Source adapters."""

from __future__ import annotations

from lexwatch.config import Settings
from lexwatch.sources.base import BillRecord, SourceAdapter, SourceError

__all__ = ["BillRecord", "SourceAdapter", "SourceError", "default_adapters"]


def default_adapters(settings: Settings) -> list[SourceAdapter]:
    """Adapters in merge order: baseline datasets first, chd.lu delta last."""
    from lexwatch.sources.chd import ChdAdapter
    from lexwatch.sources.datapublic import Dataset112Adapter, Dataset121Adapter

    adapters: list[SourceAdapter] = [
        Dataset112Adapter(settings.datapublic_api_url),
        Dataset121Adapter(settings.datapublic_api_url),
    ]
    if settings.chd_enabled:
        adapters.append(ChdAdapter(settings.chd_request_delay_seconds))
    return adapters
