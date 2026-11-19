"""Merge, precedence and history logic of ingest.merge.upsert_bills."""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import select

from lexwatch.ingest.merge import upsert_bills
from lexwatch.models import Bill, BillEvent
from lexwatch.sources.base import BillRecord

T112 = datetime(2024, 7, 15, tzinfo=UTC)
T121 = datetime(2025, 4, 10, tzinfo=UTC)
TVOTE_OLD = datetime(2023, 1, 1, tzinfo=UTC)
TVOTE_NEW = datetime(2026, 7, 15, tzinfo=UTC)


def rec(number="8000", source="datapublic-112", ts=T112, **kw) -> BillRecord:
    base = dict(
        type="projet",
        title="Projet de loi portant test",
        status="in_committee",
        filing_date=date(2024, 1, 2),
        author="Monsieur X, Ministre",
        dossier_url=f"https://www.chd.lu/fr/dossier/{number}",
    )
    base.update(kw)
    return BillRecord(number=number, source=source, source_updated_at=ts, **base)


def events(db, number="8000"):
    return db.scalars(
        select(BillEvent).where(BillEvent.bill_number == number).order_by(BillEvent.id)
    ).all()


def test_new_bill_is_added_without_events(db):
    stats = upsert_bills(db, [rec()], None)
    assert (stats.added, stats.changed, stats.unchanged) == (1, 0, 0)
    bill = db.get(Bill, "8000")
    assert bill.title == "Projet de loi portant test"
    assert bill.score_state == "pending"
    assert bill.source == "datapublic-112"
    assert bill.source_updated_at == T112
    assert bill.first_seen_at is not None and bill.last_changed_at is not None
    assert events(db) == []


def test_same_run_overlay_folds_in_memory(db):
    stats = upsert_bills(
        db,
        [rec(), rec(source="datapublic-121", ts=T121, status="published", author="X (Min.)")],
        None,
    )
    assert stats.added == 1
    bill = db.get(Bill, "8000")
    assert (bill.status, bill.author, bill.source) == ("published", "X (Min.)", "datapublic-121")
    assert bill.source_updated_at == T121
    assert events(db) == []


def test_identical_rerun_is_unchanged(db):
    upsert_bills(db, [rec()], None)
    stats = upsert_bills(db, [rec()], None)
    assert (stats.added, stats.changed, stats.unchanged) == (0, 0, 1)
    assert events(db) == []


def test_status_change_writes_event_but_does_not_rescore(db):
    upsert_bills(db, [rec()], None)
    bill = db.get(Bill, "8000")
    bill.score_state = "scored"
    before = bill.last_changed_at
    db.flush()

    stats = upsert_bills(db, [rec(source="datapublic-121", ts=T121, status="published")], None)
    assert stats.changed == 1
    assert bill.status == "published"
    assert bill.score_state == "scored"  # status-only change: no rescore
    assert bill.last_changed_at >= before
    [event] = events(db)
    assert (event.field, event.old_value, event.new_value, event.source) == (
        "status",
        "in_committee",
        "published",
        "datapublic-121",
    )


def test_title_change_rescores(db):
    upsert_bills(db, [rec()], None)
    db.get(Bill, "8000").score_state = "scored"
    db.flush()
    upsert_bills(db, [rec(source="datapublic-121", ts=T121, title="Projet de loi modifié")], None)
    bill = db.get(Bill, "8000")
    assert bill.title == "Projet de loi modifié"
    assert bill.score_state == "pending"
    assert [e.field for e in events(db)] == ["title"]


def test_whitespace_only_title_difference_is_ignored(db):
    upsert_bills(db, [rec(title="Projet de loi\nportant  test")], None)
    stats = upsert_bills(db, [rec(ts=T121, title="Projet de loi portant test")], None)
    assert stats.unchanged == 1
    assert db.get(Bill, "8000").title == "Projet de loi\nportant  test"


def test_dates_are_recorded_as_iso_text(db):
    upsert_bills(db, [rec()], None)
    upsert_bills(db, [rec(ts=T121, status="published", closing_date=date(2025, 1, 31))], None)
    by_field = {e.field: e for e in events(db)}
    assert by_field["closing_date"].old_value is None
    assert by_field["closing_date"].new_value == "2025-01-31"


def test_none_never_blanks_a_field(db):
    upsert_bills(db, [rec(closing_date=date(2024, 5, 1))], None)
    stats = upsert_bills(
        db, [rec(ts=T121, author=None, closing_date=None, filing_date=None, title="")], None
    )
    bill = db.get(Bill, "8000")
    assert stats.unchanged == 1
    assert bill.author == "Monsieur X, Ministre"
    assert bill.closing_date == date(2024, 5, 1)
    assert bill.title == "Projet de loi portant test"


def test_older_record_does_not_overwrite_newer_state(db):
    upsert_bills(db, [rec(source="datapublic-121", ts=T121, status="published")], None)
    stats = upsert_bills(
        db,
        [rec(source="chd", ts=TVOTE_OLD, status="adopted", closing_date=date(2023, 1, 1))],
        None,
    )
    bill = db.get(Bill, "8000")
    assert stats.unchanged == 1
    assert bill.status == "published"
    assert bill.closing_date is None  # status-linked field: only a newer record may set it
    assert bill.source_updated_at == T121


def test_older_record_fills_empty_fields(db):
    upsert_bills(db, [rec(source="datapublic-121", ts=T121, author=None)], None)
    upsert_bills(db, [rec(source="datapublic-112", ts=T112, author="Madame Y")], None)
    bill = db.get(Bill, "8000")
    assert bill.author == "Madame Y"
    assert bill.source_updated_at == T121  # filling does not move the precedence clock
    [event] = events(db)
    assert (event.field, event.old_value, event.new_value) == ("author", None, "Madame Y")


def test_newer_vote_wins_status_and_closing_date(db):
    upsert_bills(db, [rec(source="datapublic-121", ts=T121)], None)
    upsert_bills(
        db,
        [
            rec(
                source="chd",
                ts=TVOTE_NEW,
                status="adopted",
                closing_date=date(2026, 7, 15),
                title="Short vote label",
                author=None,
                filing_date=None,
            )
        ],
        None,
    )
    bill = db.get(Bill, "8000")
    assert bill.status == "adopted"
    assert bill.closing_date == date(2026, 7, 15)
    assert bill.title == "Projet de loi portant test"  # vote label never replaces a title
    assert bill.source == "chd"
    assert bill.source_updated_at == TVOTE_NEW
    assert {e.field: e.source for e in events(db)} == {"status": "chd", "closing_date": "chd"}


def test_equal_timestamp_later_source_wins(db):
    upsert_bills(db, [rec(ts=T121, status="in_committee")], None)
    upsert_bills(db, [rec(source="datapublic-121", ts=T121, status="published")], None)
    assert db.get(Bill, "8000").status == "published"


def test_vote_created_bill_gets_dataset_title_later(db):
    upsert_bills(
        db,
        [
            rec(
                "8800",
                source="chd",
                ts=TVOTE_NEW,
                title="Short label",
                status="adopted",
                author=None,
                filing_date=None,
            )
        ],
        None,
    )
    bill = db.get(Bill, "8800")
    assert (bill.title, bill.source, bill.score_state) == ("Short label", "chd", "pending")
    bill.score_state = "scored"
    db.flush()
    later = datetime(2027, 1, 1, tzinfo=UTC)
    upsert_bills(
        db,
        [
            rec(
                "8800",
                source="datapublic-121",
                ts=later,
                title="Projet de loi complet",
                status="published",
            )
        ],
        None,
    )
    assert bill.title == "Projet de loi complet"
    assert bill.score_state == "pending"
    assert {e.field for e in events(db, "8800")} >= {"title", "status", "author"}


def test_vote_title_fills_an_empty_title(db):
    upsert_bills(db, [rec("8801", title="")], None)
    upsert_bills(db, [rec("8801", source="chd", ts=TVOTE_NEW, title="Label")], None)
    assert db.get(Bill, "8801").title == "Label"


def test_bulk_upsert_of_thousands(db):
    records = [rec(str(n), title=f"Projet de loi {n}") for n in range(1000, 5000)]
    stats = upsert_bills(db, records, None)
    assert stats.added == 4000
    again = upsert_bills(db, records, None)
    assert again.unchanged == 4000


def test_extra_is_shallow_merged_without_events(db):
    upsert_bills(db, [rec(extra={"raw_status": "EnCommission", "documents_list": "Dépôt"})], None)
    bill = db.get(Bill, "8000")
    assert bill.extra == {"raw_status": "EnCommission", "documents_list": "Dépôt"}
    bill.score_state = "scored"
    stamp = bill.last_changed_at
    db.flush()

    docs = [{"label": "Dépôt", "date": "2024-01-02", "url": "https://x/1.pdf"}]
    stats = upsert_bills(
        db,
        [
            rec(
                source="datapublic-121",
                ts=T121,
                extra={"raw_status": "COMM", "documents": docs, "dossier_pdf_url": "https://x"},
            )
        ],
        None,
    )
    assert stats.unchanged == 1  # extra-only change is not a bill change
    assert bill.extra == {
        "raw_status": "COMM",  # newer source wins per key
        "documents_list": "Dépôt",  # keys are never dropped
        "documents": docs,
        "dossier_pdf_url": "https://x",
    }
    assert bill.score_state == "scored"
    assert bill.last_changed_at == stamp
    assert events(db) == []


def test_older_extra_only_fills_missing_keys(db):
    upsert_bills(db, [rec(source="datapublic-121", ts=T121, extra={"raw_status": "PUB_JO"})], None)
    upsert_bills(
        db,
        [rec(source="chd", ts=TVOTE_OLD, extra={"raw_status": "vote", "votes": {"Oui": 3}})],
        None,
    )
    db.expire_all()
    assert db.get(Bill, "8000").extra == {"raw_status": "PUB_JO", "votes": {"Oui": 3}}
