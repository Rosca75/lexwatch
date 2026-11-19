"""QA (independent) tests for the data.public.lu adapters (datasets 112 and 121).

Written against docs/build-prompt.md section 3, DECISIONS.md and docs/research/datapublic.md,
not against the implementation. Bugs are marked ``xfail(strict=True, reason="BUG: ...")``.
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime

import httpx
import openpyxl
import pytest

from lexwatch.sources.base import BILL_STATUSES, BillRecord, ResourceState, SourceError
from lexwatch.sources.datapublic import (
    Dataset112Adapter,
    Dataset121Adapter,
    parse_112_csv,
    parse_121_xlsx,
)
from tests.ingest.mockdp import API, CSV_112, FIXTURES, ID_112, ID_121, XLSX_121, DataPublicMock

SAMPLE_112 = FIXTURES / "datapublic" / "112-texte-loi.sample.csv"
SAMPLE_121 = FIXTURES / "datapublic" / "121-etat-travaux.sample.xlsx"
FULL_121 = FIXTURES / "datapublic" / "121-etat-travaux.xlsx"
TS = datetime(2024, 7, 15, 14, 38, 35, tzinfo=UTC)

HEADER_112 = (
    "NATURE,LAW_NUMBER,LAW_TYPE,LAW_DEPOSIT_DATE,LAW_EVACUATION_DATE,LAW_STATUS,"
    "LAW_TITLE,LAW_CONTENT,LAW_AUTHORS"
)


@pytest.fixture
def dp() -> DataPublicMock:
    return DataPublicMock()


def by_number(records: list[BillRecord]) -> dict[str, BillRecord]:
    out = {r.number: r for r in records}
    assert len(out) == len(records), "duplicate bill numbers in one parse"
    return out


def raw_112_rows() -> list[dict[str, str]]:
    text = SAMPLE_112.read_bytes().decode("utf-8-sig")
    return list(csv.DictReader(io.StringIO(text, newline="")))


def csv_112(*rows: str, header: str = HEADER_112, bom: bool = True, eol: str = "\n") -> bytes:
    body = eol.join((header, *rows)) + eol
    return (b"\xef\xbb\xbf" if bom else b"") + body.encode("utf-8")


def xlsx_bytes(path=SAMPLE_121, mutate=None) -> bytes:
    workbook = openpyxl.load_workbook(path)
    if mutate is not None:
        mutate(workbook)
    out = io.BytesIO()
    workbook.save(out)
    return out.getvalue()


def rename_121_header(old: str, new: str):
    def mutate(workbook):
        sheet = workbook["ETAT_TRAVAUX"]
        for cell in sheet[1]:
            if cell.value == old:
                cell.value = new
                return
        raise AssertionError(f"header {old} not in fixture")

    return mutate


# --- 112: the real sample ---------------------------------------------------------------------


def test_112_sample_keeps_exactly_the_bill_rows():
    rows = raw_112_rows()
    stats: dict = {}
    records = parse_112_csv(SAMPLE_112.read_bytes(), TS, stats)
    expected = {
        r["LAW_NUMBER"] for r in rows if r["LAW_TYPE"] in ("ProjetDeLoi", "PropositionDeLoi")
    }
    assert set(by_number(records)) == expected
    assert len(records) == 65
    assert stats["excluded_types"] == {
        "DebatOrientation": 1,
        "ProjetDeRevisionDeConstitution": 1,
        "PropositionDeRevisionConstitution": 2,
        "PropositionDeRevisionReglementCHD": 1,
    }
    # constitutional revision 3923C/3923A are not bills in lexwatch's scope
    assert not {"3923A", "3923B", "3923C"} & set(by_number(records))


def test_112_sample_bom_does_not_eat_the_first_column():
    # if the BOM were kept, the first header would be "﻿NATURE"; every record must
    # still have a number and type
    records = parse_112_csv(SAMPLE_112.read_bytes(), TS)
    assert all(r.number and r.type in ("projet", "proposition") for r in records)


def test_112_sample_suffixed_numbers_are_distinct_bills():
    records = by_number(parse_112_csv(SAMPLE_112.read_bytes(), TS))
    for number in ("3837A", "3837B", "4232A", "4232B", "4305A", "4305B", "4463A"):
        assert number in records, number
    assert records["3837A"].title != records["3837B"].title
    assert records["3837A"].dossier_url == "https://www.chd.lu/fr/dossier/3837A"
    assert records["3837B"].closing_date == date(1999, 4, 2)


def test_112_sample_multiline_cells_and_whitespace():
    records = by_number(parse_112_csv(SAMPLE_112.read_bytes(), TS))
    t = records["3837B"].title
    assert t.startswith("Projet de loi modifiant\n1. la loi modifiée du 27 novembre 1980")
    # 8395 has "Projet de loi\n1)  relatif" upstream: line break kept, double space collapsed
    assert records["8395"].title.startswith("Projet de loi\n1) relatif à la valorisation")
    for rec in records.values():
        assert rec.title == rec.title.strip()
        assert "  " not in rec.title
        assert "\n\n" not in rec.title


def test_112_sample_titles_match_raw_text_modulo_whitespace():
    """Independent cross-check of every kept row against a plain csv read."""
    records = by_number(parse_112_csv(SAMPLE_112.read_bytes(), TS))
    for row in raw_112_rows():
        rec = records.get(row["LAW_NUMBER"])
        if rec is None:
            continue
        raw = row["LAW_TITLE"].lstrip("'")
        assert rec.title.split() == raw.split(), row["LAW_NUMBER"]


def test_112_sample_stray_quote_in_content_is_stripped():
    records = by_number(parse_112_csv(SAMPLE_112.read_bytes(), TS))
    for number in ("1415", "1758", "2223", "2299"):
        documents = records[number].extra["documents_list"]
        assert not documents.startswith("'"), number
        assert documents.startswith("-"), number


def test_112_sample_fields_dates_status_author():
    records = by_number(parse_112_csv(SAMPLE_112.read_bytes(), TS))
    r = records["1415"]
    assert (r.type, r.status, r.filing_date, r.closing_date) == (
        "proposition",
        "withdrawn",
        date(1970, 3, 17),
        date(1999, 11, 30),
    )
    assert r.author == "Monsieur Léon Bollendorff, Député"
    # raw authors string is kept, not split nor de-duplicated
    assert (
        records["2299"].author == "Monsieur Jean Gremling, Député, Monsieur Jean Gremling, Député"
    )
    open_bill = records["8387"]
    assert (open_bill.status, open_bill.closing_date) == ("in_committee", None)
    assert records["8409"].status == "deposited"
    assert records["8409"].extra["raw_status"] == "Cree"
    assert all(r.source == "datapublic-112" and r.source_updated_at == TS for r in records.values())
    assert all(r.summary is None for r in records.values())
    assert all(r.status in BILL_STATUSES for r in records.values())


# --- 112: synthetic edge cases ------------------------------------------------------------------


def test_112_leading_quote_in_title_and_author_is_stripped_and_crlf_ok():
    content = csv_112(
        '"[x]","08500","ProjetDeLoi","01/02/2025","","EnCommission",'
        '"\'Projet de loi portant ""test""\nsur deux lignes","\'Document","\'Monsieur X"',
        eol="\r\n",
    )
    (rec,) = parse_112_csv(content, TS)
    assert rec.number == "8500"
    assert rec.title == 'Projet de loi portant "test"\nsur deux lignes'
    assert rec.author == "Monsieur X"
    assert rec.extra["documents_list"] == "Document"


def test_112_vide_status_is_none_and_unknown_status_is_other():
    content = csv_112(
        '"[x]","8501","ProjetDeLoi","01/02/2025","","Vide","Projet de loi a","",""',
        '"[x]","8502","PropositionDeLoi","01/02/2025","","NouveauCode","Proposition b","",""',
    )
    stats: dict = {}
    recs = by_number(parse_112_csv(content, TS, stats))
    assert recs["8501"].status is None
    assert recs["8502"].status == "other"
    assert recs["8502"].extra["raw_status"] == "NouveauCode"
    assert stats["unknown_statuses"] == {"NouveauCode": 1}
    assert recs["8501"].author is None


def test_112_truncated_download_does_not_yield_truncated_title():
    content = SAMPLE_112.read_bytes()
    full = by_number(parse_112_csv(content, TS))
    cut_at = content.index("public « Centrale".encode()) + 3
    try:
        partial = parse_112_csv(content[:cut_at], TS)
    except SourceError:
        return
    for rec in partial:
        assert rec.title == full[rec.number].title, rec.number


@pytest.mark.parametrize(
    "renamed",
    ["LAW_TITLE", "LAW_STATUS", "LAW_NUMBER", "LAW_TYPE", "LAW_DEPOSIT_DATE", "LAW_AUTHORS"],
)
def test_112_renamed_column_is_a_source_error(renamed):
    header = HEADER_112.replace(renamed, renamed + "_V2")
    content = csv_112('"[x]","8500","ProjetDeLoi","01/02/2025","","Cree","P","",""', header=header)
    with pytest.raises(SourceError) as err:
        parse_112_csv(content, TS)
    assert renamed in str(err.value)


def test_112_html_instead_of_csv_is_a_source_error():
    html = b"<!DOCTYPE html><html><head><title>Maintenance</title></head><body>x</body></html>"
    with pytest.raises(SourceError, match="112"):
        parse_112_csv(html, TS)


def test_112_lowercase_xlsx_style_header_is_rejected():
    # the 112 XLSX variant uses lowercase headers; feeding it to the CSV parser must fail loudly
    header = HEADER_112.lower()
    content = csv_112('"[x]","8500","ProjetDeLoi","01/02/2025","","Cree","P","",""', header=header)
    with pytest.raises(SourceError):
        parse_112_csv(content, TS)


# --- 121: the real files -----------------------------------------------------------------------


def test_121_sample_kept_rows_titles_and_links():
    stats: dict = {}
    records = by_number(parse_121_xlsx(SAMPLE_121.read_bytes(), TS, stats))
    assert len(records) == 50  # 45 PL + 5 PPL
    assert stats["excluded_types"] == {"DO": 1, "PRCONS": 2, "PRREG": 2, "RAC": 1}
    assert "7414B" not in records  # PRCONS
    for rec in records.values():
        assert "¯" not in rec.title
        assert not rec.title.startswith(".")
        assert rec.title.startswith(("Projet de loi", "Proposition de loi")), rec.number
        assert rec.title.count("Projet de loi Projet de loi") == 0
        assert rec.dossier_url and rec.dossier_url.startswith("https://www.chd.lu/fr/dossier/")
    r = records["7389"]
    assert r.title.startswith("Projet de loi portant approbation\n1° de l'Accord")
    assert r.dossier_url == "https://www.chd.lu/fr/dossier/7389"
    assert r.status == "published"
    assert r.closing_date == date(2019, 10, 24)  # DispVote first
    assert r.author == "J.Asselborn (Min. Affaires étrangères & européennes)"
    # title already starting with the type prefix: no double prefix, '¯' -> newline
    assert records["7403"].title.startswith("Projet de loi\nportant création de l'Office national")
    # suffixed dossiers are strings with their letter
    assert {"7258A", "7258B", "6539A", "6539B", "7323A"} <= set(records)
    assert records["7258A"].dossier_url.endswith("/7258A")


def test_121_sample_hyperlinks_become_documents():
    records = by_number(parse_121_xlsx(SAMPLE_121.read_bytes(), TS))
    with_docs = [r for r in records.values() if r.extra.get("documents")]
    assert with_docs, "hyperlinks were dropped (read_only mode?)"
    for rec in with_docs:
        for doc in rec.extra["documents"]:
            assert doc["url"].startswith("https://"), doc
            assert doc["label"]
            assert doc["date"] is None or date.fromisoformat(doc["date"])


def test_121_sample_open_bills_have_no_closing_date_and_dot_is_none():
    records = by_number(parse_121_xlsx(SAMPLE_121.read_bytes(), TS))
    for rec in records.values():
        if rec.status in ("in_committee", "deposited", None):
            assert rec.closing_date is None, rec.number
        if rec.extra["raw_status"] == ".":
            assert rec.status is None


def test_121_full_file_parses_cleanly():
    stats: dict = {}
    records = by_number(parse_121_xlsx(FULL_121.read_bytes(), TS, stats))
    assert len(records) == 959 + 94  # research: PL 959, PPL 94
    assert stats["unknown_statuses"] == {}
    assert all("¯" not in r.title for r in records.values())
    assert all(r.status in (*BILL_STATUSES, None) for r in records.values())
    assert max(int(n.rstrip("AB")) for n in records) == 8520
    assert all(r.filing_date is not None for r in records.values())  # Dépôt never empty


def test_121_and_112_agree_on_shared_filing_dates():
    full_121 = by_number(parse_121_xlsx(FULL_121.read_bytes(), TS))
    sample_112 = by_number(parse_112_csv(SAMPLE_112.read_bytes(), TS))
    shared = set(full_121) & set(sample_112)
    assert len(shared) > 10
    for n in shared:
        assert full_121[n].filing_date == sample_112[n].filing_date, n
        assert full_121[n].type == sample_112[n].type, n


@pytest.mark.parametrize("renamed", ["Etat", "Dossier", "Relatif à", "Nature", "Dépôt"])
def test_121_renamed_required_column_is_a_source_error(renamed):
    content = xlsx_bytes(mutate=rename_121_header(renamed, renamed + " (v2)"))
    with pytest.raises(SourceError) as err:
        parse_121_xlsx(content, TS)
    assert renamed in str(err.value)


@pytest.mark.parametrize("renamed", ["Auteurs", "DispVote"])
def test_121_renamed_used_column_is_a_source_error(renamed):
    content = xlsx_bytes(mutate=rename_121_header(renamed, renamed + " (v2)"))
    with pytest.raises(SourceError):
        parse_121_xlsx(content, TS)


def test_121_renamed_sheet_is_a_source_error():
    def mutate(workbook):
        workbook["ETAT_TRAVAUX"].title = "DATA"

    with pytest.raises(SourceError, match="ETAT_TRAVAUX"):
        parse_121_xlsx(xlsx_bytes(mutate=mutate), TS)


@pytest.mark.parametrize(
    "content",
    [b"<html><body>Service unavailable</body></html>", b"", b"PK\x03\x04truncated"],
    ids=["html", "empty", "truncated-zip"],
)
def test_121_not_an_xlsx_is_a_source_error(content):
    with pytest.raises(SourceError, match="121"):
        parse_121_xlsx(content, TS)


def test_121_truncated_real_file_is_a_source_error():
    content = SAMPLE_121.read_bytes()
    with pytest.raises(SourceError):
        parse_121_xlsx(content[: len(content) // 2], TS)


# --- resource resolution ------------------------------------------------------------------------


def _res(rid, title, fmt, lm, rtype="main", checksum=None, created="2023-01-17T12:36:53+00:00"):
    return {
        "id": rid,
        "title": title,
        "format": fmt,
        "type": rtype,
        "last_modified": lm,
        "created_at": created,
        "checksum": {"type": "sha1", "value": checksum} if checksum else None,
    }


def test_latest_main_csv_is_selected_among_many(dp):
    content = SAMPLE_112.read_bytes()
    dp.datasets[ID_112]["resources"] = [
        _res("old-csv", "112-texte-loi.csv", "csv", "2024-07-15T14:38:35+00:00"),
        _res("new-csv", "112-texte-loi.csv", "csv", "2025-02-01T10:00:00+00:00"),
        _res("doc-csv", "112-texte-loi.csv", "csv", "2026-01-01T00:00:00+00:00", "documentation"),
        _res("upd-csv", "112-texte-loi.csv", "csv", "2026-02-01T00:00:00+00:00", "update"),
        _res("new-xlsx", "112-texte-loi.xlsx", "xlsx", "2026-03-01T00:00:00+00:00"),
        _res("new-xml", "112-texte-loi.xml", "xml", "2026-03-01T00:00:00+00:00"),
    ]
    dp.files = {rid: content for rid in ("old-csv", "new-csv", "doc-csv", "upd-csv")}
    adapter = Dataset112Adapter(API, 0)
    records = adapter.fetch(dp.client())
    assert records
    assert adapter.state.resource_id == "new-csv"
    assert adapter.state.last_modified == datetime(2025, 2, 1, 10, tzinfo=UTC)
    downloads = dp.downloads()
    assert len(downloads) == 1 and downloads[0].endswith("/new-csv")
    # every record carries the selected resource's last_modified
    assert {r.source_updated_at for r in records} == {adapter.state.last_modified}


def test_uppercase_format_and_timestamps_without_timezone(dp):
    dp.datasets[ID_112]["resources"] = [
        _res("a", "112-texte-loi.csv", "CSV", "2025-02-01T10:00:00"),
        _res("b", "112-texte-loi.csv", "csv", "2025-01-01T10:00:00+00:00"),
    ]
    dp.files = {"a": SAMPLE_112.read_bytes(), "b": SAMPLE_112.read_bytes()}
    adapter = Dataset112Adapter(API, 0)
    adapter.fetch(dp.client())
    assert adapter.state.resource_id == "a"


def test_equal_last_modified_breaks_tie_on_created_at(dp):
    lm = "2025-02-01T10:00:00+00:00"
    dp.datasets[ID_112]["resources"] = [
        _res("older", "112-texte-loi.csv", "csv", lm, created="2023-01-01T00:00:00+00:00"),
        _res("newer", "112-texte-loi.csv", "csv", lm, created="2024-01-01T00:00:00+00:00"),
    ]
    dp.files = {"older": SAMPLE_112.read_bytes(), "newer": SAMPLE_112.read_bytes()}
    adapter = Dataset112Adapter(API, 0)
    adapter.fetch(dp.client())
    assert adapter.state.resource_id == "newer"


def test_121_ignores_csv_and_non_main_resources(dp):
    dp.datasets[ID_121]["resources"] = [
        _res(XLSX_121, "121-etat-travaux.xlsx", "xlsx", "2025-04-10T09:41:52+00:00"),
        _res("csv-export", "121-etat-travaux.csv", "csv", "2026-01-01T00:00:00+00:00"),
        _res("doc", "LISTE_COLONNES.xlsx", "xlsx", "2026-01-01T00:00:00+00:00", "documentation"),
    ]
    adapter = Dataset121Adapter(API, 0)
    assert adapter.fetch(dp.client())
    assert adapter.state.resource_id == XLSX_121


@pytest.mark.parametrize("resources", [[], None, [{"format": "csv", "type": "main"}]])
def test_no_usable_resource_is_a_source_error(dp, resources):
    dp.datasets[ID_112]["resources"] = (
        [_res("x", "112-texte-loi.xlsx", "xlsx", "2025-01-01T00:00:00+00:00")]
        if resources is None
        else resources
    )
    with pytest.raises(SourceError, match="datapublic-112"):
        Dataset112Adapter(API, 0).fetch(dp.client())
    assert dp.downloads() == []


# --- API / download failures --------------------------------------------------------------------


def _fail_api(status: int):
    def handler(request: httpx.Request):
        if request.url.path.startswith("/api/1/datasets/"):
            return httpx.Response(status, text="error")
        return None

    return handler


@pytest.mark.parametrize("adapter_cls", [Dataset112Adapter, Dataset121Adapter])
@pytest.mark.parametrize("status", [404, 500, 503])
def test_api_http_error_is_a_source_error(dp, adapter_cls, status):
    dp.on_request = _fail_api(status)
    with pytest.raises(SourceError) as err:
        adapter_cls(API, 0).fetch(dp.client())
    assert str(status) in str(err.value)
    assert dp.downloads() == []


@pytest.mark.parametrize("adapter_cls", [Dataset112Adapter, Dataset121Adapter])
def test_api_timeout_is_a_source_error(dp, adapter_cls):
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    dp.on_request = handler
    with pytest.raises(SourceError, match="(?i)timeout|timed out"):
        adapter_cls(API, 0).fetch(dp.client())


def test_download_timeout_is_a_source_error(dp):
    def handler(request):
        if request.url.host == "download.data.public.lu":
            raise httpx.ConnectTimeout("connect timed out", request=request)
        return None

    dp.on_request = handler
    with pytest.raises(SourceError):
        Dataset112Adapter(API, 0).fetch(dp.client())


def test_api_invalid_json_is_a_source_error(dp):
    def handler(request):
        if request.url.path.startswith("/api/1/datasets/"):
            return httpx.Response(200, text="<html>not json</html>")
        return None

    dp.on_request = handler
    with pytest.raises(SourceError, match="(?i)json"):
        Dataset112Adapter(API, 0).fetch(dp.client())


@pytest.mark.parametrize("payload", [[], "maintenance", 42])
def test_api_json_not_an_object_is_a_source_error(dp, payload):
    def handler(request):
        if request.url.path.startswith("/api/1/datasets/"):
            return httpx.Response(200, json=payload)
        return None

    dp.on_request = handler
    with pytest.raises(SourceError):
        Dataset112Adapter(API, 0).fetch(dp.client())


@pytest.mark.parametrize("status", [404, 500])
def test_download_http_error_is_a_source_error(dp, status):
    dp.fail["/fr/datasets/r/"] = status
    with pytest.raises(SourceError, match=str(status)):
        Dataset112Adapter(API, 0).fetch(dp.client())


@pytest.mark.parametrize(
    ("adapter_cls", "rid"), [(Dataset112Adapter, CSV_112), (Dataset121Adapter, XLSX_121)]
)
def test_download_returns_html_page_is_a_source_error(dp, adapter_cls, rid):
    dp.files[rid] = b"<!DOCTYPE html><html><body><h1>Erreur 502</h1></body></html>"
    with pytest.raises(SourceError):
        adapter_cls(API, 0).fetch(dp.client())


# --- unchanged-resource skip --------------------------------------------------------------------


def _known(adapter, dp):
    """Resolve once and return the state, as the pipeline would store it."""
    adapter.fetch(dp.client())
    return adapter.state


def test_same_checksum_skips_download_even_if_last_modified_moved(dp):
    first = Dataset112Adapter(API, 0)
    state = _known(first, dp)
    dp.calls.clear()
    dp.set_resource(ID_112, CSV_112, last_modified="2026-05-05T00:00:00+00:00")
    second = Dataset112Adapter(API, 0)
    second.known_state = state
    assert second.fetch(dp.client()) == []
    assert second.unchanged is True
    assert dp.downloads() == []


def test_changed_checksum_downloads(dp):
    first = Dataset112Adapter(API, 0)
    state = _known(first, dp)
    dp.calls.clear()
    dp.set_resource(ID_112, CSV_112, checksum={"type": "sha1", "value": "0" * 40})
    second = Dataset112Adapter(API, 0)
    second.known_state = state
    assert second.fetch(dp.client())
    assert second.unchanged is False
    assert len(dp.downloads()) == 1


def test_new_resource_id_with_same_checksum_downloads(dp):
    real = _known(Dataset112Adapter(API, 0), dp)
    dp.calls.clear()
    adapter = Dataset112Adapter(API, 0)
    adapter.known_state = ResourceState("another-id", real.last_modified, real.checksum)
    assert adapter.fetch(dp.client())
    assert len(dp.downloads()) == 1


def test_no_checksum_falls_back_to_last_modified(dp):
    dp.set_resource(ID_112, CSV_112, checksum=None)
    state = _known(Dataset112Adapter(API, 0), dp)
    assert state.checksum is None
    dp.calls.clear()
    same = Dataset112Adapter(API, 0)
    same.known_state = state
    assert same.fetch(dp.client()) == [] and same.unchanged
    dp.set_resource(ID_112, CSV_112, last_modified="2026-01-01T00:00:00+00:00")
    moved = Dataset112Adapter(API, 0)
    moved.known_state = state
    assert moved.fetch(dp.client()) and not moved.unchanged


def test_skip_check_needs_the_api_but_not_the_file(dp):
    """Even when unchanged, the API is asked (so a new resource is noticed)."""
    state = _known(Dataset121Adapter(API, 0), dp)
    dp.calls.clear()
    adapter = Dataset121Adapter(API, 0)
    adapter.known_state = state
    adapter.fetch(dp.client())
    assert any("/api/1/datasets/" in c for c in dp.calls)
    assert not any("/fr/datasets/r/" in c for c in dp.calls)
