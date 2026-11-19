"""QA (independent) tests for run_ingestion: failure isolation, skip, idempotency, rescore.

Requirements: build-prompt section 3 ("If the parse fails or the page structure changes, log a
clear error, mark the run as failed ... and keep the existing data"), DECISIONS.md (source
precedence) and docs/ingestion.md. Bugs are marked ``xfail(strict=True, reason="BUG: ...")``.
"""

from __future__ import annotations

import io

import httpx
import openpyxl
import pytest
from sqlalchemy import delete, func, select

from lexwatch.ingest.pipeline import run_ingestion
from lexwatch.models import Bill, BillEvent, SourceState
from lexwatch.sources.chd import ChdAdapter
from lexwatch.sources.datapublic import Dataset112Adapter, Dataset121Adapter
from tests.ingest.mockdp import (
    API,
    CSV_109,
    CSV_112,
    ID_112,
    ID_121,
    SLUG_109,
    XLSX_121,
    DataPublicMock,
)

NAMES = ("datapublic-112", "datapublic-121", "chd")
TARGETS = {
    "datapublic-112": (ID_112, CSV_112),
    "datapublic-121": (ID_121, XLSX_121),
    "chd": (SLUG_109, CSV_109),
}
HTML = b"<!DOCTYPE html><html><head><title>Maintenance</title></head><body>503</body></html>"
VOTE_HEADER = (
    "meeting_date,vote_name,vote_type,person_title,lastname,firstname,"
    "rattachement_abrv,rattachement_type,vote_result"
)


def adapters():
    return [Dataset112Adapter(API, 0), Dataset121Adapter(API, 0), ChdAdapter(API, 0)]


@pytest.fixture
def dp() -> DataPublicMock:
    return DataPublicMock()


def snapshot(db) -> dict:
    db.expire_all()
    return {
        b.number: (
            b.type,
            b.title,
            b.status,
            b.filing_date,
            b.closing_date,
            b.author,
            b.dossier_url,
            b.summary,
            b.source,
            b.score_state,
            b.last_changed_at,
            b.source_updated_at,
            b.extra,
        )
        for b in db.scalars(select(Bill))
    }


def n_events(db) -> int:
    return db.scalar(select(func.count()).select_from(BillEvent))


def bump_all(dp: DataPublicMock, tag: str = "bumped") -> None:
    """Publish 'new' checksums for every resource so the next run downloads again."""
    for name, (dataset, rid) in TARGETS.items():
        algo = "md5" if name == "chd" else "sha1"
        dp.set_resource(dataset, rid, checksum={"type": algo, "value": f"{tag}-{name}"})


def refs(dp: DataPublicMock, dataset: str) -> set[str]:
    data = dp.datasets[dataset]
    return {dataset, str(data.get("id")), str(data.get("slug"))}


def rename_header(dp: DataPublicMock, target: str) -> None:
    dataset, rid = TARGETS[target]
    if target == "datapublic-121":
        workbook = openpyxl.load_workbook(io.BytesIO(dp.files[rid]))
        for cell in workbook["ETAT_TRAVAUX"][1]:
            if cell.value == "Etat":
                cell.value = "Statut"
        out = io.BytesIO()
        workbook.save(out)
        dp.files[rid] = out.getvalue()
    else:
        column = b"LAW_TITLE" if target == "datapublic-112" else b"vote_name"
        dp.files[rid] = dp.files[rid].replace(column, column + b"_V2", 1)


def inject(dp: DataPublicMock, target: str, kind: str) -> None:
    dataset, rid = TARGETS[target]
    api_refs = refs(dp, dataset)

    def is_api(request: httpx.Request) -> bool:
        path = request.url.path
        return path.startswith("/api/1/datasets/") and path.split("/")[4] in api_refs

    def handler(request: httpx.Request):
        if kind.startswith("api-") and is_api(request):
            if kind == "api-404":
                return httpx.Response(404, json={"message": "Not found"})
            if kind == "api-500":
                return httpx.Response(500, text="Internal Server Error")
            if kind == "api-timeout":
                raise httpx.ReadTimeout("read timed out", request=request)
            if kind == "api-invalid-json":
                return httpx.Response(200, text="<html>oops</html>")
        if kind == "download-500" and request.url.path == f"/fr/datasets/r/{rid}":
            return httpx.Response(500, text="boom")
        return None

    dp.on_request = handler
    if kind == "download-html":
        dp.files[rid] = HTML
    if kind == "missing-column":
        rename_header(dp, target)


FAILURES = [
    "api-404",
    "api-500",
    "api-timeout",
    "api-invalid-json",
    "download-500",
    "download-html",
    "missing-column",
]
HINTS = {
    "api-404": "404",
    "api-500": "500",
    "api-timeout": "timeout",
    "api-invalid-json": "json",
    "download-500": "500",
    "missing-column": "missing",
}


@pytest.mark.parametrize("kind", FAILURES)
@pytest.mark.parametrize("target", NAMES)
def test_failure_is_isolated_reported_and_keeps_existing_data(db, dp, target, kind):
    first = run_ingestion(db, None, adapters(), dp.client())
    assert first.failed_sources == []
    before = snapshot(db)
    states_before = {s.name: (s.resource_id, s.checksum) for s in db.scalars(select(SourceState))}

    bump_all(dp)
    inject(dp, target, kind)
    result = run_ingestion(db, None, adapters(), dp.client())

    assert result.failed_sources == [target]
    info = result.sources[target]
    assert info["status"] == "failed" and info["records"] == 0
    error = info["error"]
    assert isinstance(error, str) and len(error) > 10
    if kind in HINTS:
        assert HINTS[kind] in error.lower(), error
    for other in NAMES:
        if other != target:
            assert result.sources[other]["status"] == "ok", (other, result.sources[other])
            assert result.sources[other]["records"] > 0
    # nothing deleted, nothing changed (the other sources re-sent identical content)
    assert snapshot(db) == before
    assert n_events(db) == 0
    state = db.get(SourceState, target)
    assert state.status == "failed" and state.error
    assert (state.resource_id, state.checksum) == states_before[target]


def test_failure_then_recovery_downloads_again(db, dp):
    run_ingestion(db, None, adapters(), dp.client())
    bump_all(dp)
    inject(dp, "datapublic-121", "download-500")
    run_ingestion(db, None, adapters(), dp.client())
    dp.on_request = None
    dp.calls.clear()
    result = run_ingestion(db, None, adapters(), dp.client())
    assert result.sources["datapublic-121"]["status"] == "ok"
    assert result.sources["datapublic-112"]["status"] == "unchanged"
    assert result.sources["chd"]["status"] == "unchanged"
    assert [c for c in dp.downloads() if XLSX_121 in c]
    assert db.get(SourceState, "datapublic-121").status == "ok"
    assert db.get(SourceState, "datapublic-121").error is None


def test_first_run_with_a_failing_source_still_loads_the_others(db, dp):
    inject(dp, "datapublic-112", "api-500")
    result = run_ingestion(db, None, adapters(), dp.client())
    assert result.failed_sources == ["datapublic-112"]
    assert result.added > 0
    sources = {b.source for b in db.scalars(select(Bill))}
    assert "datapublic-112" not in sources and "datapublic-121" in sources


# --- skip ---------------------------------------------------------------------------------------


def test_unchanged_checksums_skip_all_downloads(db, dp):
    run_ingestion(db, None, adapters(), dp.client())
    dp.calls.clear()
    result = run_ingestion(db, None, adapters(), dp.client())
    assert {n: s["status"] for n, s in result.sources.items()} == dict.fromkeys(NAMES, "unchanged")
    assert dp.downloads() == []
    assert not any("/fr/datasets/r/" in c for c in dp.calls)
    assert (result.added, result.changed) == (0, 0)
    assert all(s.status == "unchanged" for s in db.scalars(select(SourceState)))


def test_only_the_changed_source_is_downloaded(db, dp):
    run_ingestion(db, None, adapters(), dp.client())
    dp.calls.clear()
    dp.set_resource(ID_121, XLSX_121, checksum={"type": "sha1", "value": "new"})
    result = run_ingestion(db, None, adapters(), dp.client())
    assert result.sources["datapublic-121"]["status"] == "ok"
    assert result.sources["datapublic-112"]["status"] == "unchanged"
    downloads = dp.downloads()
    assert len(downloads) == 1 and downloads[0].endswith(XLSX_121)


# --- idempotency --------------------------------------------------------------------------------


def test_forced_rerun_of_identical_files_changes_nothing(db, dp):
    run_ingestion(db, None, adapters(), dp.client())
    for bill in db.scalars(select(Bill)):
        bill.score_state = "scored"
    db.flush()
    before = snapshot(db)
    # forget what was fetched: everything is downloaded and merged again
    db.execute(delete(SourceState))
    db.flush()
    result = run_ingestion(db, None, adapters(), dp.client())
    assert {s["status"] for s in result.sources.values()} == {"ok"}
    assert (result.added, result.changed) == (0, 0)
    assert result.unchanged == len(before)
    assert n_events(db) == 0
    assert snapshot(db) == before


def test_three_identical_runs_write_no_events(db, dp):
    for i in range(3):
        bump_all(dp, tag=f"run{i}")
        result = run_ingestion(db, None, adapters(), dp.client())
        assert result.failed_sources == []
        if i:
            assert (result.added, result.changed) == (0, 0)
    assert n_events(db) == 0


# --- status vs title change through the whole pipeline ------------------------------------------


def _vote_rows(meeting: str, name: str, yes: int = 3, no: int = 1) -> bytes:
    lines = []
    for i, result in enumerate(["Oui"] * yes + ["Non"] * no):
        lines.append(
            f'"{meeting}","{name}","Premier vote constitutionnel","Monsieur","D{i}","T","CSV",'
            f'"Groupe politique","{result}"'
        )
    return ("\n".join(lines) + "\n").encode("cp1252")


def test_new_vote_changes_status_only_and_does_not_rescore(db, dp):
    run_ingestion(db, None, adapters(), dp.client())
    bill = db.get(Bill, "8516")  # 121: PPL in committee
    assert bill is not None and bill.status == "in_committee"
    for b in db.scalars(select(Bill)):
        b.score_state = "scored"
    db.flush()
    title = bill.title
    dp.files[CSV_109] += _vote_rows("2026-10-01 15:00:00", "PPL 8516 - Code civil")
    dp.set_resource(SLUG_109, CSV_109, checksum={"type": "md5", "value": "v2"})
    result = run_ingestion(db, None, adapters(), dp.client())
    assert result.changed == 1
    db.expire_all()
    bill = db.get(Bill, "8516")
    assert (bill.status, bill.title, bill.score_state) == ("adopted", title, "scored")
    got = {e.field: (e.old_value, e.new_value, e.source) for e in bill.events}
    assert got == {
        "status": ("in_committee", "adopted", "chd"),
        "closing_date": (None, "2026-10-01", "chd"),
    }
    pending = db.scalar(select(func.count()).select_from(Bill).where(Bill.score_state == "pending"))
    assert pending == 0


def test_new_121_title_triggers_rescore_with_history(db, dp):
    run_ingestion(db, None, adapters(), dp.client())
    for b in db.scalars(select(Bill)):
        b.score_state = "scored"
    db.flush()
    old_title = db.get(Bill, "8516").title
    workbook = openpyxl.load_workbook(io.BytesIO(dp.files[XLSX_121]))
    sheet = workbook["ETAT_TRAVAUX"]
    header = [c.value for c in sheet[1]]
    col_n, col_t = header.index("Dossier"), header.index("Relatif à")
    for row in sheet.iter_rows(min_row=2):
        if str(row[col_n].value) == "8516":
            row[col_t].value = ".portant réforme complète du Code civil"
    out = io.BytesIO()
    workbook.save(out)
    dp.files[XLSX_121] = out.getvalue()
    dp.set_resource(
        ID_121,
        XLSX_121,
        checksum={"type": "sha1", "value": "v2"},
        last_modified="2026-11-01T09:00:00+00:00",
    )
    result = run_ingestion(db, None, adapters(), dp.client())
    assert result.changed == 1
    db.expire_all()
    bill = db.get(Bill, "8516")
    assert bill.title == "Proposition de loi portant réforme complète du Code civil"
    assert bill.score_state == "pending"
    title_events = [e for e in bill.events if e.field == "title"]
    assert len(title_events) == 1
    assert (title_events[0].old_value, title_events[0].source) == (old_title, "datapublic-121")


# --- vote label vs real title through the pipeline ----------------------------------------------


def _add_vote_for_8516(dp: DataPublicMock) -> None:
    dp.files[CSV_109] += _vote_rows("2026-07-01 15:00:00", "PPL 8516 - Code civil")


def test_vote_label_does_not_replace_121_title_in_one_run(db, dp):
    _add_vote_for_8516(dp)
    run_ingestion(db, None, adapters(), dp.client())
    bill = db.get(Bill, "8516")
    assert bill.title.startswith("Proposition de loi portant modification")
    assert bill.status == "adopted"  # vote (2026-07) newer than 121 (2025-04)
    assert n_events(db) == 0


@pytest.mark.xfail(
    strict=True,
    reason="BUG: when 121 fails on the run that first sees a vote, the bill is created with "
    "the vote label and the 121 title (older than the vote) never replaces it afterwards",
)
def test_vote_label_is_replaced_once_121_recovers(db, dp):
    _add_vote_for_8516(dp)
    inject(dp, "datapublic-121", "api-500")
    run_ingestion(db, None, adapters(), dp.client())
    assert db.get(Bill, "8516").title == "Code civil"
    dp.on_request = None
    run_ingestion(db, None, adapters(), dp.client())
    db.expire_all()
    assert db.get(Bill, "8516").title.startswith("Proposition de loi portant modification")
