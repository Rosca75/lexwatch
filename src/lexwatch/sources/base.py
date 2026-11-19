"""Source adapter contract (ingestion stream).

An adapter fetches bills from one upstream source and returns normalized ``BillRecord``s.
Adapters never touch the database; ``ingest.merge.upsert_bills`` does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

import httpx


class SourceError(Exception):
    """The source could not be fetched or its structure is not what the parser expects.

    Raised by ``SourceAdapter.fetch``. The pipeline catches it per adapter, records it in
    ``runs.details["sources"][name]`` and keeps existing data untouched.
    """


@dataclass(slots=True)
class BillRecord:
    number: str  # normalized bill number, e.g. "8123"; the merge key
    type: str | None = None  # "projet" | "proposition" (normalized by adapters)
    title: str = ""
    status: str | None = None
    filing_date: date | None = None
    closing_date: date | None = None
    author: str | None = None  # author or ministry
    dossier_url: str | None = None
    summary: str | None = None
    source: str = ""  # adapter name, e.g. "datapublic-112", "chd"
    source_updated_at: datetime | None = None  # when the source says the record changed
    extra: dict[str, Any] = field(default_factory=dict)  # raw extras worth keeping


@runtime_checkable
class SourceAdapter(Protocol):
    name: str

    def fetch(self, client: httpx.Client) -> list[BillRecord]:
        """Fetch all records this source offers. Raise ``SourceError`` on structure failure."""
        ...
