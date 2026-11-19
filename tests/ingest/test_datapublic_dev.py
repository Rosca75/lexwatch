"""Datasets 112 and 121: resolution, download, parsing, status mapping (fixtures only)."""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime

import httpx
import pytest

from lexwatch.sources.base import (
    BILL_STATUSES,
    ResourceState,
    SourceError,
)
from lexwatch.sources.datapublic import (
    EXCLUDED_112_TYPES,
    KEPT_112_TYPES,
    REQUIRED_121_COLUMNS,
    STATUS_112,
    STATUS_121,
    Dataset112Adapter,
    Dataset121Adapter,
    parse_112_csv,
    parse_121_xlsx,
    title_from_121,
)
from tests.ingest.mockdp import API, CSV_112, FIXTURES, ID_112, ID_121, XLSX_121, DataPublicMock

LM_112 = datetime(2024, 7, 15, 14, 38, 35, 877000, tzinfo=UTC)
LM_121 = datetime(2025, 4, 10, 9, 41, 52, 258000, tzinfo=UTC)


@pytest.fixture
def dp() -> DataPublicMock:
    return DataPublicMock()


# --- 112 -------------------------------------------------------------------------------------


def test_112_fetch_resolves_csv_and_downloads_via_stable_alias(dp):
    adapter = Dataset112Adapter(API, request_delay_seconds=0)
    records = adapter.fetch(dp.client())
    assert dp.calls[0] == f"{API}datasets/{ID_112}/"
    assert dp.calls[1] == f"https://data.public.lu/fr/datasets/r/{CSV_112}"
    assert len(dp.downloads()) == 1
    sha1 = hashlib.sha1(dp.files[CSV_112]).hexdigest()
    assert adapter.state == ResourceState(CSV_112, LM_112, f"sha1:{sha1}")
    assert adapter.unchanged is False
    assert records and all(r.source == "datapublic-112" for r in records)
    assert all(r.source_updated_at == LM_112 for r in records)


def test_112_parse_sample():
    stats: dict = {}
    raw = (FIXTURES / "datapublic" / "112-texte-loi.sample.csv").read_bytes()
    records = {r.number: r for r in parse_112_csv(raw, LM_112, stats)}
    assert stats["rows"] == 70
    assert stats["kept"] == len(records)
    assert set(stats["excluded_types"]) <= set(EXCLUDED_112_TYPES)
    assert all(r.type in ("projet", "proposition") for r in records.values())
    assert all(r.extra["raw_type"] in KEPT_112_TYPES for r in records.values())

    old = records["1129"]
    assert old.type == "proposition"
    assert old.status == "withdrawn"
    assert old.extra["raw_status"] == "Retire"
    assert old.filing_date == date(1965, 6, 1)
    assert old.closing_date == date(1999, 11, 30)
    assert old.author == "Monsieur Robert Krieps, Député"
    assert old.dossier_url == "https://www.chd.lu/fr/dossier/1129"
    assert old.summary is None
    assert old.title.startswith("Proposition de loi portant modification")

    # leading Excel quote stripped from the multi-line document list
    assert records["1415"].extra["documents_list"].startswith("-Exposé des motifs\n-Texte")

    recent = records["8410"]
    assert recent.status == "deposited"
    assert recent.closing_date is None
    assert recent.filing_date == date(2024, 7, 4)
    # suffixed numbers keep their letter
    assert any(n.endswith(("A", "B")) for n in records)


def test_112_multiline_title_kept_and_cleaned():
    raw = (
        "﻿NATURE,LAW_NUMBER,LAW_TYPE,LAW_DEPOSIT_DATE,LAW_EVACUATION_DATE,LAW_STATUS,"
        "LAW_TITLE,LAW_CONTENT,LAW_AUTHORS\n"
        '"[x]",0042,ProjetDeLoi,01/02/2020,,EnCommission,'
        '"Projet de loi portant\n  1. création   ;\n\n2. autre",,"Madame A, Ministre"\n'
        '"[x]",43,ProjetDeReglementGrandDucal,01/02/2020,,Cree,"Règlement",,\n'
        '"[x]",44,PropositionDeLoi,01/02/2020,,Bizarre,"Proposition de loi x",,\n'
    ).encode()
    stats: dict = {}
    records = parse_112_csv(raw, None, stats)
    assert [r.number for r in records] == ["42", "44"]
    assert records[0].title == "Projet de loi portant\n1. création ;\n2. autre"
    assert records[0].status == "in_committee"
    assert records[0].extra.get("documents_list") is None
    assert records[1].status == "other" and records[1].extra["raw_status"] == "Bizarre"
    assert stats["excluded_types"] == {"ProjetDeReglementGrandDucal": 1}
    assert stats["unknown_statuses"] == {"Bizarre": 1}


def test_112_header_change_is_a_source_error():
    with pytest.raises(SourceError, match="missing columns"):
        parse_112_csv(b"NUMBER,TYPE\n1,ProjetDeLoi\n", None)


def test_112_not_utf8_is_a_source_error():
    with pytest.raises(SourceError, match="UTF-8"):
        parse_112_csv("LAW_NUMBER\nÉ".encode("cp1252"), None)


def test_unchanged_resource_is_not_downloaded(dp):
    adapter = Dataset112Adapter(API, request_delay_seconds=0)
    sha1 = hashlib.sha1(dp.files[CSV_112]).hexdigest()
    adapter.known_state = ResourceState(CSV_112, LM_112, f"sha1:{sha1}")
    assert adapter.fetch(dp.client()) == []
    assert adapter.unchanged is True
    assert dp.downloads() == []
    assert len(dp.calls) == 1  # only the API call


def test_changed_checksum_is_downloaded(dp):
    adapter = Dataset112Adapter(API, request_delay_seconds=0)
    adapter.known_state = ResourceState(CSV_112, LM_112, "sha1:old")
    assert adapter.fetch(dp.client())
    assert adapter.unchanged is False
    assert len(dp.downloads()) == 1


def test_highest_last_modified_resource_wins(dp):
    newer = dict(dp.datasets[ID_112]["resources"][3])
    newer.update(id="new-csv", title="112-texte-loi.csv", last_modified="2026-01-01T00:00:00+00:00")
    dp.datasets[ID_112]["resources"].append(newer)
    dp.files["new-csv"] = dp.files[CSV_112]
    adapter = Dataset112Adapter(API, request_delay_seconds=0)
    adapter.fetch(dp.client())
    assert adapter.state.resource_id == "new-csv"


def test_dataset_slug_fallback(dp):
    data = dp.datasets.pop(ID_112)
    dp.datasets["liste-des-projets-et-propositions-de-lois"] = {**data, "id": "other"}
    adapter = Dataset112Adapter(API, request_delay_seconds=0)
    assert adapter.fetch(dp.client())
    assert dp.calls[1].endswith("/datasets/liste-des-projets-et-propositions-de-lois/")


def test_missing_resource_format_is_a_source_error(dp):
    for res in dp.datasets[ID_112]["resources"]:
        res["format"] = "pdf"
    with pytest.raises(SourceError, match="no main csv resource"):
        Dataset112Adapter(API, request_delay_seconds=0).fetch(dp.client())


def test_http_error_is_a_source_error(dp):
    dp.fail["/fr/datasets/r/"] = 503
    with pytest.raises(SourceError, match="HTTP 503"):
        Dataset112Adapter(API, request_delay_seconds=0).fetch(dp.client())


# --- 121 -------------------------------------------------------------------------------------


def test_121_fetch_and_parse_sample(dp):
    adapter = Dataset121Adapter(API, request_delay_seconds=0)
    records = {r.number: r for r in adapter.fetch(dp.client())}
    assert dp.calls[0] == f"{API}datasets/{ID_121}/"
    assert adapter.state.resource_id == XLSX_121
    assert adapter.state.last_modified == LM_121
    assert adapter.stats["kept"] == len(records)
    assert all(r.source_updated_at == LM_121 for r in records.values())

    bill = records["7389"]
    assert bill.type == "projet"
    assert bill.status == "published"
    assert bill.extra["raw_status"] == "PUB_JO"
    assert bill.title.startswith("Projet de loi portant approbation\n1° de l'Accord")
    assert "¯" not in bill.title
    assert bill.filing_date == date(2018, 12, 4)
    assert bill.closing_date == date(2019, 10, 24)  # DispVote, like 112's evacuation date
    assert bill.author == "J.Asselborn (Min. Affaires étrangères & européennes)"
    assert bill.dossier_url == "https://www.chd.lu/fr/dossier/7389"
    assert bill.summary is None
    extra = bill.extra
    assert extra["dossier_pdf_url"].endswith("/7389_Dossier_Complet.pdf")
    assert extra["committee"] == ["AEECA"]
    assert extra["committee_url"] == "https://www.chd.lu/fr/commission/681"
    assert extra["committee_name"].startswith("C. Affaires étrangères")
    assert extra["author_url"] == "https://www.chd.lu/fr/deputy/49"
    assert extra["dates"]["Publication"] == "2019-10-28"
    assert all(set(d) == {"label", "date", "url"} for d in extra["documents"])
    steps = {d["label"]: d for d in extra["documents"]}
    assert steps["Dépôt"]["url"].startswith("https://wdocs-pub.chd.lu/docs/exped/")
    assert steps["Premier vote"]["date"] == "2019-10-10"
    assert extra["dates"]["Vote1"] == "2019-10-10"

    # every kept row is a PL/PPL; others are counted as excluded
    assert {r.extra["raw_type"] for r in records.values()} <= {"PL", "PPL"}
    assert all(r.status is None or r.status in BILL_STATUSES for r in records.values())


def test_121_open_bill_has_no_closing_date(dp):
    records = parse_121_xlsx(dp.files[XLSX_121], LM_121)
    open_bills = [r for r in records if r.status in ("in_committee", "deposited")]
    assert open_bills
    assert all(r.closing_date is None for r in open_bills)


def test_121_multiple_authors_joined():
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ETAT_TRAVAUX"
    header = ["Dossier", "Nature", "Relatif à", "Etat", "Dépôt", "Auteurs", "Vote1", "co_co"]
    header += [c for c in REQUIRED_121_COLUMNS if c not in header]  # all read columns
    ws.append(header)
    ws.append(
        [
            "8001",
            "PPL",
            ".relative au test",
            "VOTE_KO",
            datetime(2023, 1, 2),
            "S.Clement|M.Goergen",
            datetime(2023, 5, 6),
            "X",
        ]
    )
    ws.append([8002, "PRGD", ".règlement", "COMM", datetime(2023, 1, 2), None, None, None])
    ws.append(
        [8003, "PL", "Projet de loi déjà préfixé", "NEW", datetime(2023, 1, 2), None, None, None]
    )
    buf = io.BytesIO()
    wb.save(buf)
    stats: dict = {}
    records = parse_121_xlsx(buf.getvalue(), None, stats)
    assert [r.number for r in records] == ["8001", "8003"]
    first = records[0]
    assert first.author == "S.Clement; M.Goergen"
    assert first.title == "Proposition de loi relative au test"
    assert first.status == "rejected" and first.closing_date == date(2023, 5, 6)
    assert first.dossier_url == "https://www.chd.lu/fr/dossier/8001"  # fallback pattern
    assert first.extra["dossier_pdf_url"].endswith("/8001_Dossier_Complet.pdf")
    assert records[1].title == "Projet de loi déjà préfixé"
    assert records[1].status == "other" and records[1].extra["raw_status"] == "NEW"
    assert stats["excluded_types"] == {"PRGD": 1}


def test_121_missing_sheet_or_columns():
    import io

    import openpyxl

    wb = openpyxl.Workbook()
    wb.active.title = "Other"
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(SourceError, match="ETAT_TRAVAUX"):
        parse_121_xlsx(buf.getvalue(), None)
    wb.active.title = "ETAT_TRAVAUX"
    wb.active.append(["Dossier", "Nature"])
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(SourceError, match="missing columns"):
        parse_121_xlsx(buf.getvalue(), None)
    with pytest.raises(SourceError, match="cannot open"):
        parse_121_xlsx(b"not a zip", None)


def test_title_from_121():
    assert (
        title_from_121(".portant ¯1° a ;¯2° b", "projet") == "Projet de loi portant\n1° a ;\n2° b"
    )
    assert title_from_121(None, "projet") == ""
    assert title_from_121(".modifiant x", "proposition") == "Proposition de loi modifiant x"


# --- status mapping coverage -------------------------------------------------------------------

KNOWN_112 = [
    "Publie",
    "Retire",
    "EnCommission",
    "Vide",
    "Transforme",
    "VoteAccepte",
    "EvacueConjointement",
    "VoteRefuse",
    "Cree",
    "Fusionne",
    "AviseParConferencePresidents",
    "EnAttenteDispenseSecondVote",
]
KNOWN_121 = [
    "PUB_JO",
    "COMM",
    "RETIRE",
    ".",
    "VOTE_OK",
    "TRANSFO",
    "VOTE_KO",
    "CREE",
    "AVIS.CP",
    "2EME.VOTE",
]


def test_every_known_status_code_is_mapped():
    assert set(KNOWN_112) == set(STATUS_112)
    assert set(KNOWN_121) == set(STATUS_121)
    for value in [*STATUS_112.values(), *STATUS_121.values()]:
        assert value is None or value in BILL_STATUSES


def test_112_and_121_crosswalk_agree():
    pairs = {
        "Cree": "CREE",
        "EnCommission": "COMM",
        "VoteAccepte": "VOTE_OK",
        "VoteRefuse": "VOTE_KO",
        "Publie": "PUB_JO",
        "Retire": "RETIRE",
        "Transforme": "TRANSFO",
        "AviseParConferencePresidents": "AVIS.CP",
        "EnAttenteDispenseSecondVote": "2EME.VOTE",
        "Vide": ".",
    }
    for code_112, code_121 in pairs.items():
        assert STATUS_112[code_112] == STATUS_121[code_121]


def test_checksum_mismatch_is_a_source_error(dp):
    """A truncated/corrupted download never reaches the parser."""

    def handler(request):
        if request.url.host == "download.data.public.lu":
            return httpx.Response(200, content=dp.files[CSV_112][:-500])
        return None

    dp.on_request = handler
    with pytest.raises(SourceError, match="checksum mismatch"):
        Dataset112Adapter(API, request_delay_seconds=0).fetch(dp.client())


def test_112_truncated_inside_a_quoted_cell_is_a_source_error():
    raw = (FIXTURES / "datapublic" / "112-texte-loi.sample.csv").read_bytes()
    cut = raw.rindex(b'"Projet de loi') + 20
    with pytest.raises(SourceError, match="truncated"):
        parse_112_csv(raw[:cut], LM_112)


def test_api_json_list_is_a_source_error(dp):
    dp.on_request = lambda request: (
        httpx.Response(200, json=[]) if "/api/1/" in request.url.path else None
    )
    with pytest.raises(SourceError, match="expected an object"):
        Dataset112Adapter(API, request_delay_seconds=0).fetch(dp.client())
