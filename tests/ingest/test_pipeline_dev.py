"""run_ingestion: end to end against the fake data.public.lu, failure isolation, skip."""

from __future__ import annotations

from collections import Counter

import pytest
from sqlalchemy import func, select

from lexwatch.ingest.pipeline import run_ingestion
from lexwatch.models import Bill, BillEvent, SourceState
from lexwatch.sources import default_adapters
from lexwatch.sources.base import BillRecord, SourceError
from lexwatch.sources.chd import ChdAdapter
from lexwatch.sources.datapublic import Dataset112Adapter, Dataset121Adapter
from tests.helpers import make_test_settings
from tests.ingest.mockdp import API, CSV_109, DataPublicMock


def adapters():
    return [
        Dataset112Adapter(API, 0),
        Dataset121Adapter(API, 0),
        ChdAdapter(API, 0),
    ]


@pytest.fixture
def dp() -> DataPublicMock:
    return DataPublicMock()


def test_full_run_then_unchanged_run(db, dp):
    result = run_ingestion(db, None, adapters(), dp.client())
    assert result.failed_sources == []
    assert {n: s["status"] for n, s in result.sources.items()} == {
        "datapublic-112": "ok",
        "datapublic-121": "ok",
        "chd": "ok",
    }
    assert result.added == db.scalar(select(func.count()).select_from(Bill))
    assert result.added > 60
    assert db.scalar(select(func.count()).select_from(BillEvent)) == 0
    assert result.sources["datapublic-112"]["stats"]["kept"] > 0
    assert result.sources["chd"]["records"] == 6

    states = {s.name: s for s in db.scalars(select(SourceState))}
    assert set(states) == {"datapublic-112", "datapublic-121", "chd"}
    assert states["chd"].resource_id == CSV_109 and states["chd"].status == "ok"

    # 121 overlays 112 for shared numbers (newer snapshot)
    bill = db.get(Bill, "7389")
    assert bill.source == "datapublic-121"
    assert bill.status == "published"
    # bills only known from votes are created with their vote title
    voted = db.get(Bill, "8800")
    assert (voted.status, voted.title, voted.source) == ("adopted", "Sécurité des écoles", "chd")
    assert Counter(b.score_state for b in db.scalars(select(Bill))) == {"pending": result.added}

    # second run: every resource unchanged -> no download, nothing merged
    downloads_before = len(dp.downloads())
    again = run_ingestion(db, None, adapters(), dp.client())
    assert {s["status"] for s in again.sources.values()} == {"unchanged"}
    assert len(dp.downloads()) == downloads_before
    assert (again.added, again.changed) == (0, 0)


def test_new_resource_version_is_merged_with_events(db, dp):
    run_ingestion(db, None, adapters(), dp.client())
    # 109 regenerated with a vote of a bill that 121 shows in committee
    in_committee = db.scalars(
        select(Bill).where(Bill.status == "in_committee").order_by(Bill.number)
    ).first()
    assert in_committee is not None
    extra = (
        f'"2026-10-01 15:00:00","PL {in_committee.number} - Vote final",'
        '"Premier vote constitutionnel","Madame","A","B","X","Groupe politique","Oui"\r\n'
    ).encode("cp1252")
    dp.files[CSV_109] += extra
    dp.set_resource(
        "liste-des-votes-en-seances-plenieres",
        CSV_109,
        checksum={"type": "md5", "value": "changed"},
        last_modified="2026-10-02T04:30:00+00:00",
    )
    result = run_ingestion(db, None, adapters(), dp.client())
    assert result.sources["chd"]["status"] == "ok"
    assert result.sources["datapublic-112"]["status"] == "unchanged"
    assert result.changed == 1
    db.refresh(in_committee)
    assert in_committee.status == "adopted"
    assert in_committee.title != "Vote final"
    fields = {e.field: e for e in in_committee.events}
    assert fields["status"].old_value == "in_committee"
    assert fields["status"].source == "chd"


def test_failing_source_is_isolated_and_keeps_data(db, dp):
    run_ingestion(db, None, adapters(), dp.client())
    count = db.scalar(select(func.count()).select_from(Bill))
    state_before = db.get(SourceState, "datapublic-121").checksum

    dp.fail["/datasets/67f775f1adf67ff4c46f1b37/"] = 500
    dp.fail["/datasets/etat-des-travaux/"] = 500
    dp.set_resource(
        "63c6884fcfa8113ec4793d13",
        "0a85459a-7a17-4d84-bd72-a95347be688d",
        checksum={"type": "sha1", "value": "new"},
    )
    result = run_ingestion(db, None, adapters(), dp.client())
    assert result.failed_sources == ["datapublic-121"]
    info = result.sources["datapublic-121"]
    assert info["status"] == "failed" and "HTTP 500" in info["error"]
    assert result.sources["datapublic-112"]["status"] == "ok"
    assert result.sources["chd"]["status"] == "unchanged"
    assert db.scalar(select(func.count()).select_from(Bill)) == count
    state = db.get(SourceState, "datapublic-121")
    assert state.status == "failed" and "HTTP 500" in state.error
    assert state.checksum == state_before  # failure keeps the last good file identity


class Boom:
    name = "boom"

    def fetch(self, client):
        raise RuntimeError("unexpected")


class Static:
    name = "static"

    def __init__(self, records):
        self.records = records

    def fetch(self, client):
        return self.records


def test_unexpected_exception_and_plain_adapters(db, dp):
    records = [BillRecord(number="9001", type="projet", title="Projet de loi x", source="static")]
    result = run_ingestion(db, None, [Boom(), Static(records)], dp.client())
    assert result.sources["boom"] == {"status": "failed", "records": 0, "error": "unexpected"}
    assert result.sources["static"] == {"status": "ok", "records": 1}
    assert result.added == 1


def test_source_error_message_is_recorded(db, dp):
    class Broken:
        name = "broken"

        def fetch(self, client):
            raise SourceError("broken: header changed")

    result = run_ingestion(db, None, [Broken()], dp.client())
    assert result.sources["broken"]["error"] == "broken: header changed"


def test_failed_state_does_not_skip_next_time(db, dp):
    dp.fail["/fr/datasets/r/"] = 502
    first = run_ingestion(db, None, [Dataset112Adapter(API, 0)], dp.client())
    assert first.failed_sources == ["datapublic-112"]
    dp.fail.clear()
    second = run_ingestion(db, None, [Dataset112Adapter(API, 0)], dp.client())
    assert second.sources["datapublic-112"]["status"] == "ok"
    assert second.added > 0


def test_default_adapters_respect_chd_enabled():
    names = [a.name for a in default_adapters(make_test_settings())]
    assert names == ["datapublic-112", "datapublic-121", "chd"]
    names = [a.name for a in default_adapters(make_test_settings(chd_enabled=False))]
    assert names == ["datapublic-112", "datapublic-121"]


def test_chd_disabled_is_reported_as_skipped(db, dp, monkeypatch):
    import lexwatch.config

    settings = make_test_settings(chd_enabled=False, datapublic_request_delay_seconds=0.0)
    monkeypatch.setattr(lexwatch.config, "get_settings", lambda: settings)
    result = run_ingestion(db, None, None, dp.client())
    assert result.sources["chd"]["status"] == "skipped"
    assert result.sources["datapublic-112"]["status"] == "ok"
