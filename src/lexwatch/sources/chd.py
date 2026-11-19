"""chd.lu adapter for the daily delta (new bills, status changes, dossier links).

Owned by stream 1 (ingestion). Polite fetching: one request at a time, a delay of
``request_delay_seconds`` between requests, the configured User-Agent (set on the client),
and a cache of what was already fetched. Isolated so it can be removed once dataset 112
updates daily. Raise ``SourceError`` when the page structure changes.
"""

from __future__ import annotations

import httpx

from lexwatch.sources.base import BillRecord

SOURCE_CHD = "chd"


class ChdAdapter:
    name = SOURCE_CHD

    def __init__(self, request_delay_seconds: float = 2.0) -> None:
        self.request_delay_seconds = request_delay_seconds

    def fetch(self, client: httpx.Client) -> list[BillRecord]:
        raise NotImplementedError("stream 1: chd.lu adapter")
