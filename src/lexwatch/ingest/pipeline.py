"""Ingestion pipeline: run each adapter, merge results, record per-source status.

Owned by stream 1. One failing adapter must not stop the others nor delete data; its error is
recorded in ``IngestResult.sources[adapter.name] = {"status": "failed", "error": "..."}``
(the job copies it to ``run.details["sources"]``) and in the ``source_state`` table.

Per-source status: ``ok`` (downloaded and parsed), ``unchanged`` (same resource and checksum
as the last successful run, download skipped, no records), ``failed`` and ``skipped`` (the
adapter is disabled, e.g. ``CHD_ENABLED=false``).

All records of the successful adapters are merged in one ``upsert_bills`` call, in adapter
order (112 -> 121 -> chd), so precedence is resolved in memory before anything is written.
Nothing is committed here; the caller owns the transaction, so ``source_state`` and the bills
it describes are committed (or rolled back) together.

Bootstrap: an ingestion that adds bills to an empty ``bills`` table (the first run after a
fresh install) sets ``IngestResult.bootstrap`` and ``run.details["bootstrap"] = true``. The
digest ignores bills first seen, changed or matched during such a run: the whole history
arrives at once and none of it is news.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from lexwatch.ingest.merge import upsert_bills
from lexwatch.models import Bill, Run, SourceState
from lexwatch.sources.base import (
    SOURCE_FAILED,
    SOURCE_OK,
    SOURCE_SKIPPED,
    SOURCE_UNCHANGED,
    BillRecord,
    ResourceState,
    SourceAdapter,
)

log = logging.getLogger(__name__)

SOURCE_CHD = "chd"


@dataclass(slots=True)
class IngestResult:
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    bootstrap: bool = False  # bills were added to an empty bills table
    # name -> {"status": "ok"|"unchanged"|"failed"|"skipped", "records": n, "error": str, ...}
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def failed_sources(self) -> list[str]:
        return [name for name, info in self.sources.items() if info.get("status") == "failed"]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _load_states(session: Session) -> dict[str, SourceState]:
    return {s.name: s for s in session.scalars(select(SourceState))}


def _save_state(
    session: Session,
    states: dict[str, SourceState],
    name: str,
    status: str,
    resolved: ResourceState | None,
    error: str | None,
    now: datetime,
) -> None:
    row = states.get(name)
    if row is None:
        row = SourceState(name=name)
        session.add(row)
        states[name] = row
    if resolved is not None and status in (SOURCE_OK, SOURCE_UNCHANGED):
        # Only a successful fetch moves the "last seen file"; a failure keeps it so the next
        # run does not skip a file it never parsed.
        row.resource_id = resolved.resource_id
        row.last_modified = resolved.last_modified
        row.checksum = resolved.checksum
    row.status = status
    row.error = error
    row.fetched_at = now


def run_ingestion(
    session: Session,
    run: Run | None,
    adapters: Sequence[SourceAdapter] | None = None,
    client: httpx.Client | None = None,
) -> IngestResult:
    """Fetch from ``adapters`` (default: ``sources.default_adapters(get_settings())``)
    using ``client`` (default: ``http.make_client(settings)``) and merge into the DB."""
    result = IngestResult()
    settings = None
    if adapters is None or client is None:
        from lexwatch.config import get_settings

        settings = get_settings()
    if adapters is None:
        from lexwatch.sources import default_adapters

        assert settings is not None
        adapters = default_adapters(settings)
        if not settings.chd_enabled:
            result.sources[SOURCE_CHD] = {"status": SOURCE_SKIPPED, "records": 0}
    own_client = client is None
    if client is None:
        from lexwatch.http import make_client

        assert settings is not None
        client = make_client(settings)

    states = _load_states(session)
    collected: list[BillRecord] = []
    try:
        for adapter in adapters:
            name = adapter.name
            previous = states.get(name)
            if hasattr(adapter, "known_state"):
                adapter.known_state = (  # type: ignore[attr-defined]
                    ResourceState(previous.resource_id, previous.last_modified, previous.checksum)
                    if previous is not None and previous.status in (SOURCE_OK, SOURCE_UNCHANGED)
                    else None
                )
            now = datetime.now(UTC)
            try:
                records = adapter.fetch(client)
            except Exception as exc:  # SourceError or anything unexpected: isolate the adapter
                message = str(exc) or repr(exc)
                log.error(
                    "ingest.source_failed",
                    extra={
                        "source": name,
                        "error": message,
                        "error_type": type(exc).__name__,
                        "run_id": run.id if run is not None else None,
                    },
                )
                result.sources[name] = {"status": SOURCE_FAILED, "records": 0, "error": message}
                _save_state(session, states, name, SOURCE_FAILED, None, message[:2000], now)
                continue
            resolved: ResourceState | None = getattr(adapter, "state", None)
            status = SOURCE_UNCHANGED if getattr(adapter, "unchanged", False) else SOURCE_OK
            info: dict[str, Any] = {"status": status, "records": len(records)}
            if resolved is not None:
                info["resource_id"] = resolved.resource_id
                info["last_modified"] = _iso(resolved.last_modified)
            stats = getattr(adapter, "stats", None)
            if stats:
                info["stats"] = stats
            result.sources[name] = info
            _save_state(session, states, name, status, resolved, None, now)
            log.info("ingest.source_done", extra={"source": name, **info})
            collected.extend(records)
    finally:
        if own_client:
            client.close()

    empty = session.scalar(select(Bill.number).limit(1)) is None
    merged = upsert_bills(session, collected, run)
    result.added, result.changed, result.unchanged = merged.added, merged.changed, merged.unchanged
    result.bootstrap = empty and result.added > 0
    if result.bootstrap and run is not None:
        run.details = {**(run.details or {}), "bootstrap": True}
    session.flush()
    log.info(
        "ingest.done",
        extra={
            "added": result.added,
            "changed": result.changed,
            "unchanged": result.unchanged,
            "failed_sources": result.failed_sources,
            "bootstrap": result.bootstrap,
            "run_id": run.id if run is not None else None,
        },
    )
    return result
