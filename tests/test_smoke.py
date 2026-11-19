"""Skeleton smoke tests: migrations, app factory, health, home page, i18n parity, CSRF."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import inspect, text

from lexwatch.cli import build_parser
from lexwatch.config import Settings
from lexwatch.i18n import catalog, t
from lexwatch.scoring.base import Scorer
from lexwatch.scoring.factory import get_scorer
from lexwatch.web.app import create_app
from tests.conftest import csrf_token


def test_migrations_create_schema(engine):
    tables = set(inspect(engine).get_table_names())
    assert {
        "users",
        "bills",
        "bill_events",
        "profiles",
        "rules",
        "rule_scores",
        "matches",
        "runs",
        "alembic_version",
    } <= tables


def test_search_vector_is_generated(db):
    db.execute(
        text(
            "INSERT INTO bills (number, title, source) "
            "VALUES ('9999', 'Lutte contre le blanchiment de capitaux', 'test')"
        )
    )
    hit = db.execute(
        text(
            "SELECT number FROM bills "
            "WHERE search_vector @@ plainto_tsquery('french', 'blanchiment')"
        )
    ).scalar()
    assert hit == "9999"


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["database"] is True


def test_home_with_dev_auth(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Chambre des Députés du Grand-Duché de Luxembourg" in response.text
    assert 'href="https://www.chd.lu"' in response.text


def test_admin_guard(user_client, admin_client):
    assert user_client.get("/admin").status_code == 403
    assert admin_client.get("/admin").status_code == 200


def test_csrf_required_for_unsafe_methods(client):
    assert client.post("/lang", data={"lang": "en"}).status_code == 403
    token = csrf_token(client)
    response = client.post(
        "/lang", data={"lang": "en", "csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert '<html lang="en">' in client.get("/").text


def test_dev_auth_refused_in_production(settings):
    with pytest.raises(ValueError):
        create_app(replace(settings, env="production", secret_key="x" * 32))


def test_i18n_catalogs_have_same_keys():
    assert set(catalog("fr")) == set(catalog("en"))
    assert t("nav.bills", "en") == "Bills"
    assert t("nav.bills") == "Projets de loi"


def test_fake_scorer_contract(settings: Settings):
    scorer = get_scorer(settings)
    assert isinstance(scorer, Scorer)
    scores = scorer.score_many("blanchiment de capitaux", ["Loi sur le blanchiment", "Pêche"])
    assert len(scores) == 2 and scores[0] > scores[1]
    assert all(0.0 <= s <= 1.0 for s in scores)


def test_cli_parser():
    parser = build_parser()
    assert parser.parse_args(["rescore", "--profile", "3"]).profile == 3
    assert parser.parse_args(["send-digest", "--dry-run"]).dry_run is True
