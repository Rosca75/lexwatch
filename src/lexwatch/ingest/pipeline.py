"""Ingestion pipeline: run each adapter, merge results, record per-source status.

Owned by stream 1. One failing adapter must not stop the others nor delete data; its error is
recorded in ``run.details["sources"][adapter.name] = {"status": "failed", "error": "..."}``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy.orm import Session

from lexwatch.models import Run
from lexwatch.sources.base import SourceAdapter


@dataclass(slots=True)
class IngestResult:
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    sources: dict[str, dict[str, Any]] = field(default_factory=dict)  # name -> status info

    @property
    def failed_sources(self) -> list[str]:
        return [name for name, info in self.sources.items() if info.get("status") == "failed"]


def run_ingestion(
    session: Session,
    run: Run | None,
    adapters: Sequence[SourceAdapter] | None = None,
    client: httpx.Client | None = None,
) -> IngestResult:
    """Fetch from ``adapters`` (default: ``sources.default_adapters(get_settings())``)
    using ``client`` (default: ``http.make_client(settings)``) and merge into the DB."""
    raise NotImplementedError("stream 1: ingestion pipeline")
