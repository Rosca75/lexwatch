"""Scores and thresholds use one formatter: three decimals, locale decimal separator."""

from __future__ import annotations

from decimal import Decimal

import pytest

from lexwatch.i18n import format_score
from lexwatch.scoring import service as scoring_service
from lexwatch.scoring.service import PreviewRow
from lexwatch.web.routes.rules import format_threshold
from tests.helpers import csrf_token
from tests.web.factories import dev_user, make_bill, make_match, make_profile


@pytest.mark.parametrize(
    ("value", "lang", "expected"),
    [
        (0.4567, "fr", "0,457"),
        (0.4567, "en", "0.457"),
        (0.5, "fr", "0,500"),
        (1, "en", "1.000"),
        (Decimal("0.425"), "fr", "0,425"),
        (0.0, "en", "0.000"),
        (None, "fr", ""),
    ],
)
def test_format_score(value, lang, expected):
    assert format_score(value, lang) == expected


def test_threshold_uses_the_same_format():
    assert format_threshold(Decimal("0.65"), "fr") == "0,650"
    assert format_threshold(0.425, "en") == "0.425"


def test_bills_list_and_detail_show_three_decimals(client, db):
    profile = make_profile(db, dev_user(db, "admin"), "AML", ("blanchiment",))
    bill = make_bill(db, "8700", title="Blanchiment")
    make_match(db, profile, bill, 0.81234)
    listing = client.get(f"/?profile={profile.id}").text
    assert ">0,812<" in listing
    detail = client.get("/bills/8700").text
    assert ">0,812<" in detail


def test_rules_index_shows_thresholds_with_three_decimals(client, db):
    make_profile(db, dev_user(db, "admin"), "Own", ("r",), threshold=0.65)
    make_profile(db, dev_user(db, "admin"), "Default", ("r",))
    html = client.get("/rules").text
    assert ">0,650<" in html
    assert "0,65<" not in html


def test_preview_scores_have_three_decimals(client, db, monkeypatch):
    def preview(session, scorer, rule_texts, threshold, limit=50):
        return [PreviewRow("8801", "Blanchiment", 0.4567, True, [0.4567])]

    monkeypatch.setattr(scoring_service, "preview_matches", preview)
    make_bill(db, "8801", title="Blanchiment")
    response = client.post(
        "/rules/preview",
        data={"rule_text": ["blanchiment"], "threshold": "0.1", "name": "x"},
        headers={"HX-Request": "true", "X-CSRF-Token": csrf_token(client)},
    )
    assert response.status_code == 200
    assert ">0,457<" in response.text
    assert "blanchiment : 0,457" in response.text
