"""Merge normalized records into ``bills`` and record history in ``bill_events``.

Owned by stream 1. Rules (see ``docs/ingestion.md`` for the rationale):

- The bill number is the key. Records are applied in the order given, which the pipeline sets
  to 112 -> 121 -> chd. All records of one call are first folded in memory onto the bill's
  current state, then the net difference is written once. A bill added in this call
  therefore gets no events, and a value changed twice in one run gets one event.
- Precedence: a record overwrites a field when its ``source_updated_at`` is newer than or
  equal to the bill's ``source_updated_at`` (the time of the record that last won). An older
  record only fills fields that are still empty, except ``status`` and ``closing_date``,
  which only a winning record sets (an old vote must not close a bill that a newer snapshot
  shows in committee). When a record wins, the bill's ``source_updated_at`` moves to the
  record's.
- ``None`` / ``""`` never blanks out a field.
- Title: a chd vote title (a short label taken from the vote name) is used only while the bill
  has no title. It never replaces a title, and is replaced by any 112/121 title. Titles that
  differ only in whitespace or case count as equal (no event).
- Every change to a field of ``models.TRACKED_BILL_FIELDS`` writes one ``bill_events`` row
  (field, old, new, changed_at, source of the winning record).
- New bills and changed bills get ``last_changed_at = now``. Scoring is re-triggered
  (``score_state = 'pending'``) only for new bills and when a scored input (title or summary)
  changed; status, date, author and link changes do not need a rescore.
- ``bills.source`` holds the adapter that last changed the bill.
- ``bills.extra`` (JSONB) is a shallow merge of the records' ``extra`` dicts: a key is set when
  the bill does not have it yet or when the record wins (same precedence as above); keys are
  never dropped. It is not a tracked field: no event, no rescore, and an extra-only change
  does not move ``last_changed_at`` nor count as a changed bill.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from lexwatch.models import TRACKED_BILL_FIELDS, Bill, BillEvent, Run
from lexwatch.sources.base import BillRecord

SOURCE_CHD = "chd"
# Fields whose change re-triggers scoring (the scorer reads title + summary).
SCORED_FIELDS = frozenset({"title", "summary"})
# Fields subject to precedence, in the order events are written.
MERGED_FIELDS = (
    "type",
    "title",
    "status",
    "filing_date",
    "closing_date",
    "author",
    "dossier_url",
    "summary",
)

# Fields that only a record at least as new as the bill's current state may set.
WINNER_ONLY_FIELDS = frozenset({"status", "closing_date"})

_FLOOR = datetime.min.replace(tzinfo=UTC)
_WS_RE = re.compile(r"\s+")


@dataclass(slots=True)
class MergeStats:
    added: int = 0
    changed: int = 0
    unchanged: int = 0


def utcnow() -> datetime:
    return datetime.now(UTC)


def _ts(value: datetime | None) -> datetime:
    if value is None:
        return _FLOOR
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _same(field: str, old: Any, new: Any) -> bool:
    if field == "title" and isinstance(old, str) and isinstance(new, str):
        return _WS_RE.sub(" ", old).strip().casefold() == _WS_RE.sub(" ", new).strip().casefold()
    return old == new


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


@dataclass(slots=True)
class _Draft:
    """In-memory state of one bill while records are folded onto it."""

    values: dict[str, Any]
    updated_at: datetime | None
    changed_by: dict[str, str]  # field -> source of the record that set the current value
    extra: dict[str, Any]
    last_source: str | None = None

    @classmethod
    def from_bill(cls, bill: Bill | None) -> _Draft:
        if bill is None:
            return cls(
                values=dict.fromkeys(MERGED_FIELDS), updated_at=None, changed_by={}, extra={}
            )
        return cls(
            values={f: getattr(bill, f) for f in MERGED_FIELDS},
            updated_at=bill.source_updated_at,
            changed_by={},
            extra=dict(bill.extra or {}),
        )

    def apply(self, record: BillRecord) -> None:
        wins = _ts(record.source_updated_at) >= _ts(self.updated_at)
        touched = False
        for field in MERGED_FIELDS:
            new = getattr(record, field)
            if new is None or new == "":
                continue
            old = self.values[field]
            empty = old is None or old == ""
            if field == "title" and record.source == SOURCE_CHD and not empty:
                continue  # a vote label never replaces a title
            if not wins and (not empty or field in WINNER_ONLY_FIELDS):
                continue
            if not empty and _same(field, old, new):
                continue
            self.values[field] = new
            self.changed_by[field] = record.source
            touched = True
        for key, new in (record.extra or {}).items():
            # shallow merge: a key is set when absent or when the record wins; never dropped
            if new is not None and (wins or key not in self.extra):
                self.extra[key] = new
        if wins and record.source_updated_at is not None:
            self.updated_at = record.source_updated_at
        if touched:
            self.last_source = record.source


def upsert_bills(session: Session, records: Iterable[BillRecord], run: Run | None) -> MergeStats:
    """Upsert records (does not commit, flushes). Return counts.

    Loads every existing bill once (a few thousand rows) instead of querying per record.
    ``run`` is accepted for the contract; events carry no run id.
    """
    del run  # not stored on events
    by_number: dict[str, list[BillRecord]] = {}
    for record in records:
        by_number.setdefault(record.number, []).append(record)
    stats = MergeStats()
    if not by_number:
        return stats

    existing: dict[str, Bill] = {b.number: b for b in session.scalars(select(Bill))}
    now = utcnow()
    new_bills: list[Bill] = []
    events: list[dict[str, Any]] = []

    for number, bill_records in by_number.items():
        bill = existing.get(number)
        draft = _Draft.from_bill(bill)
        for record in bill_records:
            draft.apply(record)

        if bill is None:
            new_bills.append(
                Bill(
                    number=number,
                    **{f: draft.values[f] for f in MERGED_FIELDS if f != "title"},
                    title=draft.values["title"] or "",
                    score_state="pending",
                    source=draft.last_source or bill_records[-1].source,
                    first_seen_at=now,
                    last_changed_at=now,
                    source_updated_at=draft.updated_at,
                    extra=draft.extra,
                )
            )
            stats.added += 1
            continue

        changed_fields = [
            f for f in MERGED_FIELDS if not _same(f, getattr(bill, f), draft.values[f])
        ]
        if draft.updated_at != bill.source_updated_at and draft.updated_at is not None:
            bill.source_updated_at = draft.updated_at
        if draft.extra != (bill.extra or {}):
            bill.extra = draft.extra  # not tracked, no event, no rescore, no last_changed_at
        if not changed_fields:
            stats.unchanged += 1
            continue
        for field in changed_fields:
            old, new = getattr(bill, field), draft.values[field]
            if field in TRACKED_BILL_FIELDS:
                events.append(
                    {
                        "bill_number": number,
                        "field": field,
                        "old_value": _as_text(old),
                        "new_value": _as_text(new),
                        "changed_at": now,
                        "source": draft.changed_by.get(field, draft.last_source or "unknown"),
                    }
                )
            setattr(bill, field, new)
        if SCORED_FIELDS.intersection(changed_fields):
            bill.score_state = "pending"
        bill.last_changed_at = now
        bill.source = draft.last_source or bill.source
        stats.changed += 1

    if new_bills:
        session.add_all(new_bills)
    session.flush()
    if events:
        session.execute(insert(BillEvent), events)
    return stats
