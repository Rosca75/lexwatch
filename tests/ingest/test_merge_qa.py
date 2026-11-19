"""QA (independent) tests for ingest.merge.upsert_bills: precedence, history, rescore, bulk.

Rules under test (build-prompt sections 3 and 9, DECISIONS.md "Source precedence"):
bill number is the key; 112 -> 121 -> chd; chd wins for status and dates only when newer;
a chd vote label never replaces a real title; None never blanks; every tracked change is
written to bill_events; only scored text (title, summary) triggers a rescore.
Bugs are marked ``xfail(strict=True, reason="BUG: ...")``.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import event, func, select

from lexwatch.ingest.merge import upsert_bills
from lexwatch.models import TRACKED_BILL_FIELDS, Bill, BillEvent
from lexwatch.sources.base import BillRecord

T112 = datetime(2024, 7, 15, 14, 38, 35, tzinfo=UTC)
T121 = datetime(2025, 4, 10, 9, 41, 52, tzinfo=UTC)
VOTE_BEFORE_112 = datetime(2023, 3, 1, 15, 0, tzinfo=UTC)
VOTE_BETWEEN = datetime(2024, 12, 5, 15, 0, tzinfo=UTC)  # after 112, before 121
VOTE_AFTER = datetime(2026, 7, 15, 13, 23, tzinfo=UTC)


def r112(number="8400", **kw) -> BillRecord:
    base = dict(
        type="projet",
        title="Projet de loi portant création d'un registre",
        status="in_committee",
        filing_date=date(2024, 6, 1),
        closing_date=None,
        author="Monsieur Gilles Roth, Ministre des Finances",
        dossier_url=f"https://www.chd.lu/fr/dossier/{number}",
        source="datapublic-112",
        source_updated_at=T112,
        extra={"raw_status": "EnCommission"},
    )
    base.update(kw)
    return BillRecord(number=number, **base)


def r121(number="8400", **kw) -> BillRecord:
    base = dict(
        type="projet",
        title="Projet de loi portant création d'un registre",
        status="published",
        filing_date=date(2024, 6, 1),
        closing_date=date(2025, 1, 20),
        author="G.Roth (Min. Finances)",
        dossier_url=f"https://www.chd.lu/fr/dossier/{number}",
        source="datapublic-121",
        source_updated_at=T121,
        extra={"raw_status": "PUB_JO", "committee": ["FI"]},
    )
    base.update(kw)
    return BillRecord(number=number, **base)


def rvote(number="8400", ts=VOTE_AFTER, status="adopted", title="Registre", **kw) -> BillRecord:
    base = dict(
        type="projet",
        title=title,
        status=status,
        closing_date=ts.date(),
        dossier_url=f"https://www.chd.lu/fr/dossier/{number}",
        source="chd",
        source_updated_at=ts,
        extra={"raw_status": f"Premier vote constitutionnel: {status}"},
    )
    base.update(kw)
    return BillRecord(number=number, **base)


def events(db, number="8400") -> list[BillEvent]:
    return list(
        db.scalars(
            select(BillEvent)
            .where(BillEvent.bill_number == number)
            .order_by(BillEvent.field, BillEvent.id)
        )
    )


def event_map(db, number="8400") -> dict[str, tuple[str | None, str | None, str]]:
    evs = events(db, number)
    out = {e.field: (e.old_value, e.new_value, e.source) for e in evs}
    assert len(out) == len(evs), "more than one event per field in one merge"
    return out


def mark_scored(db, *numbers: str) -> None:
    for n in numbers or ("8400",):
        db.get(Bill, n).score_state = "scored"
    db.flush()


@contextmanager
def statements(db):
    engine = db.get_bind().engine
    seen: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", before)


# --- precedence ---------------------------------------------------------------------------------


def test_three_sources_in_one_run_newest_wins_and_new_bill_has_no_events(db):
    stats = upsert_bills(
        db, [r112(), r121(), rvote(ts=VOTE_BETWEEN, status="rejected", title="x")], None
    )
    assert (stats.added, stats.changed) == (1, 0)
    bill = db.get(Bill, "8400")
    # the vote (Dec 2024) is older than the 121 snapshot (Apr 2025): 121 keeps status/date
    assert (bill.status, bill.closing_date) == ("published", date(2025, 1, 20))
    assert bill.title == "Projet de loi portant création d'un registre"
    assert bill.author == "G.Roth (Min. Finances)"
    assert bill.source == "datapublic-121"
    assert bill.source_updated_at == T121
    assert bill.score_state == "pending"
    assert events(db) == []


def test_source_order_in_the_batch_does_not_change_the_outcome(db):
    upsert_bills(db, [rvote(ts=VOTE_BETWEEN, status="rejected"), r121(), r112()], None)
    bill = db.get(Bill, "8400")
    assert (bill.status, bill.closing_date, bill.author) == (
        "published",
        date(2025, 1, 20),
        "G.Roth (Min. Finances)",
    )


def test_older_vote_never_overwrites_newer_snapshot_status_across_runs(db):
    upsert_bills(db, [r121(status="in_committee", closing_date=None)], None)
    mark_scored(db)
    stats = upsert_bills(db, [rvote(ts=VOTE_BETWEEN, status="adopted")], None)
    bill = db.get(Bill, "8400")
    assert (bill.status, bill.closing_date) == ("in_committee", None)
    assert events(db) == []
    assert stats.changed == 0


def test_newer_vote_wins_status_and_closing_date_with_history(db):
    upsert_bills(db, [r112(), r121(status="in_committee", closing_date=None)], None)
    mark_scored(db)
    before = db.get(Bill, "8400").last_changed_at
    stats = upsert_bills(db, [rvote(ts=VOTE_AFTER, status="adopted", title="Registre")], None)
    assert stats.changed == 1
    bill = db.get(Bill, "8400")
    assert (bill.status, bill.closing_date) == ("adopted", date(2026, 7, 15))
    # the vote label never replaces the real title
    assert bill.title == "Projet de loi portant création d'un registre"
    assert bill.score_state == "scored"  # status/date change: no rescore
    assert bill.source == "chd"
    assert bill.last_changed_at > before
    assert event_map(db) == {
        "status": ("in_committee", "adopted", "chd"),
        "closing_date": (None, "2026-07-15", "chd"),
    }


def test_older_112_refresh_does_not_undo_121(db):
    upsert_bills(db, [r121()], None)
    upsert_bills(db, [r112(status="in_committee", author="Monsieur X", title="Other title")], None)
    bill = db.get(Bill, "8400")
    assert (bill.status, bill.author, bill.title) == (
        "published",
        "G.Roth (Min. Finances)",
        "Projet de loi portant création d'un registre",
    )
    assert events(db) == []


def test_older_record_fills_empty_fields_but_not_status(db):
    upsert_bills(db, [r121(author=None, filing_date=None, status=None, closing_date=None)], None)
    upsert_bills(db, [r112(status="withdrawn", closing_date=date(2024, 7, 1))], None)
    bill = db.get(Bill, "8400")
    assert bill.author == "Monsieur Gilles Roth, Ministre des Finances"
    assert bill.filing_date == date(2024, 6, 1)
    assert bill.status is None and bill.closing_date is None
    got = event_map(db)
    assert got["author"] == (None, "Monsieur Gilles Roth, Ministre des Finances", "datapublic-112")
    assert got["filing_date"] == (None, "2024-06-01", "datapublic-112")
    assert "status" not in got


def test_equal_timestamp_later_record_wins(db):
    upsert_bills(db, [r121(status="in_committee", closing_date=None)], None)
    upsert_bills(db, [r121(status="withdrawn", source="datapublic-112")], None)
    assert db.get(Bill, "8400").status == "withdrawn"


# --- titles ------------------------------------------------------------------------------------


def test_newer_vote_label_never_replaces_a_real_title(db):
    upsert_bills(db, [r121()], None)
    mark_scored(db)
    upsert_bills(db, [rvote(title="Completely different label")], None)
    bill = db.get(Bill, "8400")
    assert bill.title == "Projet de loi portant création d'un registre"
    assert "title" not in event_map(db)
    assert bill.score_state == "scored"


def test_vote_created_bill_then_newer_dataset_title_replaces_label(db):
    upsert_bills(db, [rvote("8600", ts=VOTE_AFTER, title="Logements abordables")], None)
    bill = db.get(Bill, "8600")
    assert (bill.title, bill.status, bill.source) == ("Logements abordables", "adopted", "chd")
    mark_scored(db, "8600")
    later = datetime(2026, 12, 1, tzinfo=UTC)
    upsert_bills(
        db,
        [r121("8600", title="Projet de loi relative aux logements abordables", **_ts(later))],
        None,
    )
    assert bill.title == "Projet de loi relative aux logements abordables"
    assert bill.score_state == "pending"
    assert event_map(db, "8600")["title"] == (
        "Logements abordables",
        "Projet de loi relative aux logements abordables",
        "datapublic-121",
    )


def _ts(value: datetime) -> dict:
    return {"source_updated_at": value}


def test_vote_label_is_replaced_by_older_dataset_title(db):
    upsert_bills(db, [rvote("8600", ts=VOTE_AFTER, title="Logements abordables")], None)
    upsert_bills(db, [r121("8600", title="Projet de loi relative aux logements abordables")], None)
    bill = db.get(Bill, "8600")
    assert bill.title == "Projet de loi relative aux logements abordables"
    # status from the newer vote is kept
    assert bill.status == "adopted"


def test_vote_created_bill_gets_empty_fields_from_older_snapshot(db):
    upsert_bills(db, [rvote("8600", ts=VOTE_AFTER, title="Label")], None)
    upsert_bills(db, [r121("8600", status="in_committee", closing_date=None)], None)
    bill = db.get(Bill, "8600")
    assert bill.status == "adopted"  # newer vote kept
    assert bill.closing_date == date(2026, 7, 15)
    assert bill.author == "G.Roth (Min. Finances)"
    assert bill.filing_date == date(2024, 6, 1)


def test_whitespace_and_case_only_title_difference_is_not_a_change(db):
    upsert_bills(db, [r121()], None)
    mark_scored(db)
    variant = "  PROJET de loi   portant création\nd'un registre "
    stats = upsert_bills(db, [r121(title=variant, source_updated_at=VOTE_AFTER)], None)
    assert stats.changed == 0
    assert db.get(Bill, "8400").score_state == "scored"
    assert events(db) == []


# --- None / empty never blanks ------------------------------------------------------------------


def test_none_and_empty_never_blank_a_field(db):
    upsert_bills(db, [r121(summary="Résumé")], None)
    empty = BillRecord(
        number="8400",
        type=None,
        title="",
        status=None,
        filing_date=None,
        closing_date=None,
        author="",
        dossier_url=None,
        summary="",
        source="datapublic-121",
        source_updated_at=VOTE_AFTER,
        extra={},
    )
    stats = upsert_bills(db, [empty], None)
    bill = db.get(Bill, "8400")
    assert stats.changed == 0
    assert bill.title and bill.status == "published" and bill.author and bill.summary == "Résumé"
    assert bill.closing_date == date(2025, 1, 20) and bill.dossier_url and bill.type
    assert events(db) == []


# --- history -----------------------------------------------------------------------------------


def test_every_tracked_field_change_is_recorded_with_old_new_source(db):
    upsert_bills(db, [r112()], None)
    changed = r121(
        type="proposition",
        title="Proposition de loi nouvelle",
        status="published",
        filing_date=date(2024, 6, 2),
        closing_date=date(2025, 1, 20),
        author="F.Fayot",
        dossier_url="https://www.chd.lu/fr/dossier/8400-bis",
        summary="Un résumé",
    )
    upsert_bills(db, [changed], None)
    got = event_map(db)
    assert set(got) == set(TRACKED_BILL_FIELDS)
    assert got["type"] == ("projet", "proposition", "datapublic-121")
    assert got["title"] == (
        "Projet de loi portant création d'un registre",
        "Proposition de loi nouvelle",
        "datapublic-121",
    )
    assert got["status"] == ("in_committee", "published", "datapublic-121")
    assert got["filing_date"] == ("2024-06-01", "2024-06-02", "datapublic-121")
    assert got["closing_date"] == (None, "2025-01-20", "datapublic-121")
    assert got["author"][2] == "datapublic-121"
    assert got["summary"] == (None, "Un résumé", "datapublic-121")
    stamps = {e.changed_at for e in events(db)}
    assert len(stamps) == 1 and next(iter(stamps)) is not None


def test_events_name_the_source_that_set_each_field(db):
    upsert_bills(db, [r112()], None)
    upsert_bills(
        db,
        [
            r121(status="in_committee", closing_date=None, author="G.Roth (Min. Finances)"),
            rvote(ts=VOTE_AFTER, status="adopted"),
        ],
        None,
    )
    got = event_map(db)
    assert got["author"][2] == "datapublic-121"
    assert got["status"] == ("in_committee", "adopted", "chd")  # net change, one event
    assert got["closing_date"] == (None, "2026-07-15", "chd")


def test_value_changed_and_changed_back_in_one_run_writes_nothing(db):
    upsert_bills(db, [r121(status="in_committee", closing_date=None)], None)
    newer = datetime(2025, 6, 1, tzinfo=UTC)
    upsert_bills(
        db,
        [
            rvote(ts=newer, status="adopted", closing_date=None),
            r121(status="in_committee", closing_date=None, source_updated_at=VOTE_AFTER),
        ],
        None,
    )
    assert "status" not in event_map(db)


# --- rescore triggers ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value", "rescore"),
    [
        ("status", "withdrawn", False),
        ("closing_date", date(2025, 3, 3), False),
        ("author", "S.Wilmes (Min. Fonction publique)", False),
        ("dossier_url", "https://www.chd.lu/fr/dossier/8400B", False),
        ("filing_date", date(2024, 5, 5), False),
        ("type", "proposition", False),
        ("title", "Projet de loi portant création d'un registre national", True),
        ("summary", "Nouveau résumé", True),
    ],
)
def test_only_scored_text_triggers_a_rescore(db, field, value, rescore):
    upsert_bills(db, [r121()], None)
    mark_scored(db)
    upsert_bills(db, [r121(**{field: value, "source_updated_at": VOTE_AFTER})], None)
    bill = db.get(Bill, "8400")
    assert getattr(bill, field) == value
    assert (bill.score_state == "pending") is rescore
    assert field in event_map(db)


def test_extra_only_change_is_not_a_change(db):
    upsert_bills(db, [r121()], None)
    mark_scored(db)
    stamp = db.get(Bill, "8400").last_changed_at
    stats = upsert_bills(
        db, [r121(extra={"committee": ["JUST"]}, source_updated_at=VOTE_AFTER)], None
    )
    bill = db.get(Bill, "8400")
    assert stats.changed == 0
    assert bill.extra["committee"] == ["JUST"]
    assert bill.extra["raw_status"] == "PUB_JO"  # keys are never dropped
    assert bill.last_changed_at == stamp and bill.score_state == "scored"
    assert events(db) == []


# --- idempotency -------------------------------------------------------------------------------


def _batch() -> list[BillRecord]:
    out: list[BillRecord] = []
    for n in range(8000, 8040):
        out.append(r112(str(n)))
        if n % 2:
            out.append(r121(str(n)))
        if n % 3 == 0:
            out.append(rvote(str(n), ts=VOTE_AFTER if n % 2 else VOTE_BETWEEN))
    out.append(rvote("8900", title="Nouveau"))
    return out


def test_same_batch_twice_changes_nothing_and_writes_nothing(db):
    upsert_bills(db, _batch(), None)
    db.flush()
    db.expire_all()  # reload state from the database, as a new run would
    snapshot = {
        b.number: (b.status, b.title, b.closing_date, b.last_changed_at, b.extra)
        for b in db.scalars(select(Bill))
    }
    with statements(db) as seen:
        stats = upsert_bills(db, _batch(), None)
        db.flush()
    assert (stats.added, stats.changed) == (0, 0)
    assert stats.unchanged == len(snapshot)
    assert db.scalar(select(func.count()).select_from(BillEvent)) == 0
    writes = [s for s in seen if s.lstrip().upper().startswith(("UPDATE", "INSERT", "DELETE"))]
    assert writes == [], writes
    db.expire_all()
    after = {
        b.number: (b.status, b.title, b.closing_date, b.last_changed_at, b.extra)
        for b in db.scalars(select(Bill))
    }
    assert after == snapshot


# --- bulk ---------------------------------------------------------------------------------------


N_BULK = 5000


def _bulk_new() -> list[BillRecord]:
    return [r112(str(10000 + i), title=f"Projet de loi numéro {i}") for i in range(N_BULK)]


def test_bulk_merge_of_5000_new_records_uses_few_statements(db):
    with statements(db) as seen:
        start = time.perf_counter()
        stats = upsert_bills(db, _bulk_new(), None)
        db.flush()
        elapsed = time.perf_counter() - start
    assert stats.added == N_BULK
    assert elapsed < 5, elapsed
    assert len(seen) < 30, f"{len(seen)} statements for {N_BULK} new bills"


def test_bulk_merge_of_5000_unchanged_records_uses_few_statements(db):
    upsert_bills(db, _bulk_new(), None)
    db.flush()
    with statements(db) as seen:
        stats = upsert_bills(db, _bulk_new(), None)
        db.flush()
    assert stats.unchanged == N_BULK
    assert len(seen) < 5, f"{len(seen)} statements for {N_BULK} unchanged bills"


def test_bulk_merge_of_5000_changed_records_uses_few_statements(db):
    upsert_bills(db, _bulk_new(), None)
    db.flush()
    changed = [
        r121(str(10000 + i), title=f"Projet de loi numéro {i}", status="published")
        for i in range(N_BULK)
    ]
    with statements(db) as seen:
        start = time.perf_counter()
        stats = upsert_bills(db, changed, None)
        db.flush()
        elapsed = time.perf_counter() - start
    assert stats.changed == N_BULK
    status_events = db.scalar(
        select(func.count()).select_from(BillEvent).where(BillEvent.field == "status")
    )
    assert status_events == N_BULK
    assert len(seen) < 30, f"{len(seen)} statements for {N_BULK} changed bills"
    assert elapsed < 5, elapsed
