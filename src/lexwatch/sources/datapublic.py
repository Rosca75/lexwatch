"""data.public.lu adapters for datasets 112 (bill list) and 121 (Etat des travaux).

Owned by stream 1 (ingestion). Resolve the latest resource through the udata API
(``settings.datapublic_api_url``); never hard-code a timestamped download URL.
See docs/research/ for verified dataset ids, columns and fixtures.
"""

from __future__ import annotations

import httpx

from lexwatch.sources.base import BillRecord

SOURCE_112 = "datapublic-112"
SOURCE_121 = "datapublic-121"


class Dataset112Adapter:
    name = SOURCE_112

    def __init__(self, api_url: str = "https://data.public.lu/api/1/") -> None:
        self.api_url = api_url

    def fetch(self, client: httpx.Client) -> list[BillRecord]:
        raise NotImplementedError("stream 1: dataset 112 adapter")


class Dataset121Adapter:
    name = SOURCE_121

    def __init__(self, api_url: str = "https://data.public.lu/api/1/") -> None:
        self.api_url = api_url

    def fetch(self, client: httpx.Client) -> list[BillRecord]:
        raise NotImplementedError("stream 1: dataset 121 adapter")
