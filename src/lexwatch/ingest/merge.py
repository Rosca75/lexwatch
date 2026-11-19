"""Merge normalized records into ``bills`` and record history in ``bill_events``.

Owned by stream 1. Rules:
- the bill number is the key;
- chd.lu wins for status and dates when its ``source_updated_at`` is newer;
- every change of a field in ``models.TRACKED_BILL_FIELDS`` writes one ``BillEvent``;
- a new or changed bill gets ``score_state='pending'`` and ``last_changed_at=now``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy.orm import Session

from lexwatch.models import Run
from lexwatch.sources.base import BillRecord


@dataclass(slots=True)
class MergeStats:
    added: int = 0
    changed: int = 0
    unchanged: int = 0


def upsert_bills(session: Session, records: Iterable[BillRecord], run: Run | None) -> MergeStats:
    """Upsert records (does not commit). Return counts."""
    raise NotImplementedError("stream 1: merge/upsert")
