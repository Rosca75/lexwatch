"""chd adapter: plenary votes (dataset 109) -> bill status records."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from lexwatch.sources.base import SourceError
from lexwatch.sources.chd import ChdAdapter, parse_109_votes, parse_vote_name
from tests.ingest.mockdp import API, CSV_109, DATA, FIXTURES, SLUG_109, DataPublicMock

LUX = ZoneInfo("Europe/Luxembourg")


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("PL 8535 - Logements abordables", ("PL", "8535", "Logements abordables")),
        ("PPL 8551 - Carrières inférieures", ("PPL", "8551", "Carrières inférieures")),
        ("PL8608 Réorg. Chambre des Métiers", ("PL", "8608", "Réorg. Chambre des Métiers")),
        (
            "PL 6539B - Procédure de dissolution adm.",
            ("PL", "6539B", "Procédure de dissolution adm."),
        ),
        ("PL 8355", ("PL", "8355", "")),
        ("Projet de loi N°7871", ("PL", "7871", "")),
        ("Projet de loi 7964 - Covid-19", ("PL", "7964", "Covid-19")),
        ("Proposition de loi 7789", ("PPL", "7789", "")),
        (
            "PL 7942 - Amendement Art.8 Statut de Rome",
            ("PL", "7942", "Amendement Art.8 Statut de Rome"),
        ),
    ],
)
def test_bill_vote_names(name, expected):
    parsed = parse_vote_name(name)
    assert parsed is not None
    assert (parsed.kind, parsed.number, parsed.title) == expected


@pytest.mark.parametrize(
    "name",
    [
        "MO1 - PL8727",
        "MO 1 - PL 8333",
        "Motion 3 - Débat europ & étrang",
        "PL 8414 - Vote séparé - article 10",
        "PL 8535 - Logements abordables - Vote séparé (article 44)",
        "PL 8142 - Amendement n°1",
        "PL 8732 - Demandeurs de protection internationale - (Amendement n°1)",
        "PL 8634 - Réforme des pensions (Amendement 1)",
        "PL 7978 - motion 1",
        "Projet de loi 7964 (vote séparé) - Covid-19",
        "PPC 7777 - Chapitres IV et Vbis de la Constitution",
        "PPMRCHD 8127 - Autonomie fonctionnelle",
        "Débat Etat de la Nation",
        "",
    ],
)
def test_non_bill_votes_are_excluded(name):
    assert parse_vote_name(name) is None


def test_parse_synthetic_votes():
    stats: dict = {}
    raw = (DATA / "109-votes.synthetic.csv").read_bytes()
    records = {r.number: r for r in parse_109_votes(raw, stats)}
    assert set(records) == {"8800", "8801", "7859", "8802", "8804", "8805"}

    adopted = records["8800"]
    assert adopted.status == "adopted"
    assert adopted.type == "projet"
    assert adopted.title == "Sécurité des écoles"  # cp1252 decoded
    assert adopted.closing_date == date(2026, 7, 15)
    assert adopted.source_updated_at == datetime(2026, 7, 15, 15, 23, 32, tzinfo=LUX)
    assert adopted.dossier_url == "https://www.chd.lu/fr/dossier/8800"
    assert adopted.source == "chd"
    assert adopted.filing_date is None and adopted.author is None
    assert adopted.extra["votes"] == {"Oui": 2, "Non": 1, "Abstention": 1, "Pas participé": 1}
    assert adopted.extra["vote_type"] == "Premier vote constitutionnel"

    rejected = records["8801"]
    assert (rejected.status, rejected.type) == ("rejected", "proposition")

    assert records["7859"].status == "adopted"  # legacy "Not Found" vote type
    assert records["7859"].title == ""

    second = records["8802"]  # second constitutional vote is later and decides
    assert second.status == "rejected"
    assert second.closing_date == date(2026, 3, 1)
    assert second.extra["vote_type"] == "Second vote constitutionnel"

    assert records["8804"].title == "Titre sans tiret"
    assert records["8805"].status == "rejected"  # tie: no majority
    assert stats["bills"] == 6


def test_head_fixture_has_only_a_motion():
    raw = (FIXTURES / "chd" / "opendata-109-votes.head.csv").read_bytes()
    stats: dict = {}
    assert parse_109_votes(raw, stats) == []
    assert stats["votes"] == 1 and stats["bill_votes"] == 0


def test_header_change_is_a_source_error():
    with pytest.raises(SourceError, match="missing columns"):
        parse_109_votes(b"date,name\n2026-01-01,PL 1234\n")


def test_adapter_resolves_dataset_109_by_slug():
    dp = DataPublicMock()
    adapter = ChdAdapter(API, request_delay_seconds=0)
    records = adapter.fetch(dp.client())
    assert dp.calls[0] == f"{API}datasets/{SLUG_109}/"
    assert dp.calls[1] == f"https://data.public.lu/fr/datasets/r/{CSV_109}"
    assert adapter.state.resource_id == CSV_109
    assert adapter.state.checksum.startswith("md5:")
    assert len(records) == 6
    # chd adapter never requests chd.lu itself
    assert not any("chd.lu" in c for c in dp.calls)
