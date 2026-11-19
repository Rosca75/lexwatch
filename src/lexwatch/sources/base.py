"""Source adapter contract (ingestion stream).

An adapter fetches bills from one upstream source and returns normalized ``BillRecord``s.
Adapters never touch the database; ``ingest.merge.upsert_bills`` does.

This module also defines the normalized vocabulary shared by every adapter: bill types and
bill statuses. Raw upstream codes are kept in ``BillRecord.extra["raw_status"]`` /
``extra["raw_type"]``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

import httpx

# --- normalized vocabulary -------------------------------------------------------------------

TYPE_PROJET = "projet"  # projet de loi (government bill)
TYPE_PROPOSITION = "proposition"  # proposition de loi (members' bill)
BILL_TYPES = (TYPE_PROJET, TYPE_PROPOSITION)

STATUS_DEPOSITED = "deposited"
STATUS_IN_COMMITTEE = "in_committee"
STATUS_COUNCIL_OPINION = "council_opinion"  # reserved: no current source code maps to it
STATUS_SECOND_VOTE_PENDING = "second_vote_pending"
STATUS_ADOPTED = "adopted"
STATUS_REJECTED = "rejected"
STATUS_PUBLISHED = "published"
STATUS_WITHDRAWN = "withdrawn"
STATUS_MERGED = "merged"
STATUS_TRANSFORMED = "transformed"
STATUS_OTHER = "other"
BILL_STATUSES = (
    STATUS_DEPOSITED,
    STATUS_IN_COMMITTEE,
    STATUS_COUNCIL_OPINION,
    STATUS_SECOND_VOTE_PENDING,
    STATUS_ADOPTED,
    STATUS_REJECTED,
    STATUS_PUBLISHED,
    STATUS_WITHDRAWN,
    STATUS_MERGED,
    STATUS_TRANSFORMED,
    STATUS_OTHER,
)
# Statuses after which the procedure is over.
CLOSED_STATUSES = frozenset(
    {STATUS_PUBLISHED, STATUS_REJECTED, STATUS_WITHDRAWN, STATUS_MERGED, STATUS_TRANSFORMED}
)

# Values of IngestResult.sources[name]["status"] and source_state.status.
SOURCE_OK = "ok"
SOURCE_UNCHANGED = "unchanged"
SOURCE_FAILED = "failed"
SOURCE_SKIPPED = "skipped"
SOURCE_STATUSES = (SOURCE_OK, SOURCE_UNCHANGED, SOURCE_FAILED, SOURCE_SKIPPED)

DOSSIER_URL_TEMPLATE = "https://www.chd.lu/fr/dossier/{number}"

_NUMBER_RE = re.compile(r"0*(\d+)\s*([A-Za-z]?)")


class SourceError(Exception):
    """The source could not be fetched or its structure is not what the parser expects.

    Raised by ``SourceAdapter.fetch``. The pipeline catches it per adapter, records it in
    ``runs.details["sources"][name]`` and keeps existing data untouched.

    ``status_code`` is the HTTP status of a non-2xx response; ``network`` is true for a
    transport failure (DNS, connect, TLS, timeout); ``reason`` is a short description of
    either (``"502 Bad Gateway"``, ``"ConnectError: ..."``).
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        network: bool = False,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.network = network
        self.reason = reason


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


@dataclass(frozen=True, slots=True)
class ResourceState:
    """Identity of a downloaded upstream file, used to skip unchanged downloads."""

    resource_id: str | None = None
    last_modified: datetime | None = None
    checksum: str | None = None  # "<algo>:<hex>", as published by the udata API

    def same_content(self, other: ResourceState | None) -> bool:
        if other is None or self.resource_id is None or self.resource_id != other.resource_id:
            return False
        if self.checksum and other.checksum:
            return self.checksum == other.checksum
        return self.last_modified is not None and self.last_modified == other.last_modified


@runtime_checkable
class SourceAdapter(Protocol):
    """An upstream source.

    Optional attributes the pipeline uses when present (see ``ingest.pipeline``):
    ``known_state`` (set before ``fetch``: the ``ResourceState`` of the last successful run),
    ``state`` (set by ``fetch``: what was resolved this time), ``unchanged`` (``fetch``
    returned nothing because ``state`` equals ``known_state``) and ``stats`` (a small dict of
    parse statistics recorded in the run details).
    """

    name: str

    def fetch(self, client: httpx.Client) -> list[BillRecord]:
        """Fetch all records this source offers. Raise ``SourceError`` on structure failure."""
        ...


def normalize_number(value: object) -> str | None:
    """``"08410"`` -> ``"8410"``, ``"3837a"`` -> ``"3837A"``, ``7389`` -> ``"7389"``.

    Returns ``None`` when the value is not a dossier number.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        value = int(value)
    match = _NUMBER_RE.fullmatch(str(value).strip())
    if not match:
        return None
    return f"{int(match.group(1))}{match.group(2).upper()}"


def dossier_url(number: str) -> str:
    """Public chd.lu dossier page. Linked to, never fetched (robots.txt disallows it)."""
    return DOSSIER_URL_TEMPLATE.format(number=number)


_HSPACE_RE = re.compile(r"[ \t\r\f\v ]+")


def clean_text(value: str | None) -> str:
    """Trim each line, collapse horizontal whitespace, drop blank lines; keep line breaks."""
    if not value:
        return ""
    lines = (_HSPACE_RE.sub(" ", line).strip() for line in value.split("\n"))
    return "\n".join(line for line in lines if line)


def number_sort_key(number: str) -> tuple[int, str]:
    match = _NUMBER_RE.fullmatch(number)
    if not match:
        return (0, number)
    return (int(match.group(1)), match.group(2))
