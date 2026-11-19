"""Source adapters."""

from __future__ import annotations

from lexwatch.config import Settings
from lexwatch.sources.base import BillRecord, SourceAdapter, SourceError

__all__ = ["BillRecord", "SourceAdapter", "SourceError", "default_adapters"]


def default_adapters(settings: Settings) -> list[SourceAdapter]:
    """Adapters in merge order: 112 (baseline), 121 (newer snapshot), chd (daily votes)."""
    from lexwatch.sources.chd import ChdAdapter
    from lexwatch.sources.datapublic import Dataset112Adapter, Dataset121Adapter

    api, delay = settings.datapublic_api_url, settings.datapublic_request_delay_seconds
    adapters: list[SourceAdapter] = [
        Dataset112Adapter(api, delay),
        Dataset121Adapter(api, delay),
    ]
    if settings.chd_enabled:
        adapters.append(ChdAdapter(api, settings.chd_request_delay_seconds))
    return adapters
