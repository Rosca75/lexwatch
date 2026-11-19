"""QA (independent) tests for the chd adapter (dataset 109 plenary votes, cp1252).

Written against DECISIONS.md, docs/research/chd.md and docs/ingestion.md. Bugs are marked
``xfail(strict=True, reason="BUG: ...")``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from lexwatch.sources.base import SourceError
from lexwatch.sources.chd import ChdAdapter, parse_109_votes
from tests.ingest.mockdp import API, CSV_109, FIXTURES, DataPublicMock

HEADER = (
    "meeting_date,vote_name,vote_type,person_title,lastname,firstname,"
    "rattachement_abrv,rattachement_type,vote_result"
)
FIRST = "Premier vote constitutionnel"
SECOND = "Second vote constitutionnel"


def rows(meeting: str, name: str, vote_type: str, results: dict[str, int]) -> list[str]:
    out = []
    i = 0
    for result, count in results.items():
        for _ in range(count):
            out.append(
                f'"{meeting}","{name}","{vote_type}","Monsieur","Dep{i}","Test","CSV",'
                f'"Groupe politique","{result}"'
            )
            i += 1
    return out


def votes_csv(*blocks: list[str], encoding: str = "cp1252", bom: bool = False) -> bytes:
    lines = [HEADER]
    for block in blocks:
        lines.extend(block)
    data = ("\n".join(lines) + "\n").encode(encoding)
    return (b"\xef\xbb\xbf" if bom else b"") + data


def parse(*blocks: list[str], **kw) -> dict:
    return {r.number: r for r in parse_109_votes(votes_csv(*blocks, **kw))}


# --- real fixture -------------------------------------------------------------------------------


def test_real_head_fixture_is_decoded_and_has_no_bill_vote():
    content = (FIXTURES / "chd" / "opendata-109-votes.head.csv").read_bytes()
    assert b"Vot\xe9" in content  # really cp1252
    stats: dict = {}
    assert parse_109_votes(content, stats) == []  # "MO1 - PL8727" is a motion
    assert stats["rows"] > 30
    assert stats["votes"] == 1
    assert stats["bill_votes"] == 0


def test_synthetic_dev_fixture_through_the_adapter():
    dp = DataPublicMock()
    adapter = ChdAdapter(API, 0)
    records = {r.number: r for r in adapter.fetch(dp.client())}
    assert adapter.state.resource_id == CSV_109
    assert records["8800"].title == "Sécurité des écoles"
    assert records["8800"].status == "adopted"
    assert "8801" in records and records["8801"].status == "rejected"  # 1 Oui vs 3 Non


# --- encoding -----------------------------------------------------------------------------------


def test_cp1252_accents_and_euro_sign():
    recs = parse(
        rows("2026-07-15 15:23:32", "PL 8800 - Sécurité des écoles", FIRST, {"Oui": 2}),
        rows("2026-07-15 16:00:00", "PL 8803 - Prime de 5 € à l'été", FIRST, {"Oui": 2}),
    )
    assert recs["8800"].title == "Sécurité des écoles"
    assert recs["8803"].title == "Prime de 5 € à l'été"


def test_utf8_with_bom_is_decoded():
    recs = parse(
        rows("2026-07-15 15:23:32", "PL 8800 - Sécurité des écoles", FIRST, {"Oui": 2}),
        encoding="utf-8",
        bom=True,
    )
    assert recs["8800"].title == "Sécurité des écoles"


@pytest.mark.xfail(
    strict=True,
    reason="BUG: a UTF-8 votes file without BOM is decoded as cp1252, giving mojibake titles "
    "('SÃ©curitÃ©') instead of trying strict UTF-8 first",
)
def test_utf8_without_bom_is_decoded():
    recs = parse(
        rows("2026-07-15 15:23:32", "PL 8800 - Sécurité des écoles", FIRST, {"Oui": 2}),
        encoding="utf-8",
    )
    assert recs["8800"].title == "Sécurité des écoles"


# --- which votes count -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "MO 2 - PL 8000",
        "MO2 - PL8000",
        "Motion relative au PL 8000",
        "PL 8000 - Loi test - Vote séparé (article 44)",
        "PL 8000 - Loi test - vote separe article 3",
        "PL 8000 - Amendement n°3",
        "PL 8000 - Loi test (Amendement 1)",
        "PPC 8000 - Proposition de révision",
        "Débat d'orientation sur le PL 8000",
    ],
)
def test_non_bill_votes_never_produce_a_record(name):
    assert parse(rows("2026-07-15 15:00:00", name, FIRST, {"Oui": 5})) == {}


def test_vote_type_vote_is_ignored_even_with_a_bill_name():
    assert parse(rows("2026-07-15 15:00:00", "PL 8000 - Loi test", "Voté", {"Oui": 5})) == {}


@pytest.mark.parametrize(
    ("name", "number", "bill_type", "title"),
    [
        ("PL 8000 - Loi test", "8000", "projet", "Loi test"),
        ("PL8000 - Loi test", "8000", "projet", "Loi test"),
        ("PPL 8001 - Congé test", "8001", "proposition", "Congé test"),
        ("Projet de loi N°7859", "7859", "projet", ""),
        ("Proposition de loi n° 8002 - Test", "8002", "proposition", "Test"),
        ("PL 7258A - Logement", "7258A", "projet", "Logement"),
        ("PL 8627", "8627", "projet", ""),
        ("PL 7942 - Amendement Art.8 Statut de Rome", "7942", "projet", None),
    ],
)
def test_bill_vote_names(name, number, bill_type, title):
    recs = parse(rows("2026-07-15 15:00:00", name, FIRST, {"Oui": 3, "Non": 1}))
    assert set(recs) == {number}
    rec = recs[number]
    assert rec.type == bill_type
    if title is not None:
        assert rec.title == title
    assert rec.dossier_url == f"https://www.chd.lu/fr/dossier/{number}"
    assert rec.source == "chd"
    assert rec.filing_date is None and rec.author is None


def test_legacy_not_found_vote_type_counts():
    recs = parse(rows("2021-07-15 08:00:00", "Projet de loi N°7859", "Not Found", {"Oui": 2}))
    assert recs["7859"].status == "adopted"


# --- outcome ------------------------------------------------------------------------------------


def test_tie_is_rejected_and_abstentions_do_not_count():
    recs = parse(
        rows(
            "2026-07-15 15:00:00",
            "PL 8000 - x",
            FIRST,
            {"Oui": 10, "Non": 10, "Abstention": 30, "Pas participé": 5},
        ),
        rows("2026-07-15 16:00:00", "PL 8001 - y", FIRST, {"Oui": 2, "Non": 1, "Abstention": 50}),
    )
    assert recs["8000"].status == "rejected"
    assert recs["8001"].status == "adopted"
    assert recs["8001"].extra["votes"] == {"Oui": 2, "Non": 1, "Abstention": 50}


def test_latest_vote_wins_regardless_of_row_order():
    later = rows("2026-03-01 10:00:00", "PL 8000 - x", SECOND, {"Non": 5, "Oui": 1})
    earlier = rows("2026-01-10 10:00:00", "PL 8000 - x", FIRST, {"Oui": 5})
    for blocks in ((later, earlier), (earlier, later)):
        rec = parse(*blocks)["8000"]
        assert rec.status == "rejected"
        assert rec.closing_date == date(2026, 3, 1)


def test_second_vote_beats_first_at_same_timestamp():
    first = rows("2026-03-01 10:00:00", "PL 8000 - x", FIRST, {"Oui": 5})
    second = rows("2026-03-01 10:00:00", "PL 8000 - x", SECOND, {"Non": 5})
    for blocks in ((first, second), (second, first)):
        assert parse(*blocks)["8000"].status == "rejected"


def test_vote_timestamp_is_luxembourg_local_time():
    rec = parse(rows("2026-07-15 15:23:32", "PL 8000 - x", FIRST, {"Oui": 1}))["8000"]
    assert rec.source_updated_at.tzinfo is not None
    assert rec.source_updated_at.astimezone(UTC) == datetime(2026, 7, 15, 13, 23, 32, tzinfo=UTC)
    winter = parse(rows("2026-01-15 15:00:00", "PL 8000 - x", FIRST, {"Oui": 1}))["8000"]
    assert winter.source_updated_at.astimezone(UTC) == datetime(2026, 1, 15, 14, tzinfo=UTC)
    assert rec.closing_date == date(2026, 7, 15)


def test_label_from_earlier_vote_is_kept_when_latest_has_none():
    recs = parse(
        rows("2026-01-10 10:00:00", "PL 8000 - Logements abordables", FIRST, {"Oui": 5}),
        rows("2026-03-01 10:00:00", "PL 8000", SECOND, {"Oui": 5}),
    )
    assert recs["8000"].title == "Logements abordables"


def test_bad_meeting_date_rows_are_skipped_not_fatal():
    stats: dict = {}
    content = votes_csv(
        rows("not a date", "PL 8000 - x", FIRST, {"Oui": 3}),
        rows("2026-03-01 10:00:00", "PL 8001 - y", FIRST, {"Oui": 3}),
    )
    recs = {r.number: r for r in parse_109_votes(content, stats)}
    assert set(recs) == {"8001"}
    assert stats["bad_dates"] >= 1  # counted per row


# --- structure ----------------------------------------------------------------------------------


@pytest.mark.parametrize("renamed", ["meeting_date", "vote_name", "vote_type", "vote_result"])
def test_renamed_column_is_a_source_error(renamed):
    content = votes_csv(rows("2026-03-01 10:00:00", "PL 8000 - x", FIRST, {"Oui": 1}))
    content = content.replace(renamed.encode(), (renamed + "_v2").encode(), 1)
    with pytest.raises(SourceError) as err:
        parse_109_votes(content)
    assert renamed in str(err.value)


def test_html_instead_of_csv_is_a_source_error():
    with pytest.raises(SourceError, match="chd"):
        parse_109_votes(b"<!DOCTYPE html><html><body>Erreur</body></html>")


def test_adapter_failure_on_download_is_a_source_error():
    dp = DataPublicMock()
    dp.fail["/fr/datasets/r/"] = 500
    with pytest.raises(SourceError, match="500"):
        ChdAdapter(API, 0).fetch(dp.client())


def test_adapter_never_requests_chd_lu():
    dp = DataPublicMock()
    ChdAdapter(API, 0).fetch(dp.client())
    assert dp.calls
    assert not any("chd.lu" in c for c in dp.calls)
