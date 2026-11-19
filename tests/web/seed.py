# ruff: noqa: E501
"""Seed a development database with sample data for screenshots and manual checks.

Usage (never against real data; the target database is emptied first)::

    uv run python -m tests.web.seed postgresql+psycopg://lexwatch:lexwatch@localhost:55432/lexwatch_dev_web

Then serve with ``DEV_AUTH=true SCORER=fake DATABASE_URL=... uv run lexwatch serve``.
"""

from __future__ import annotations

import sys
from datetime import UTC, date, datetime, timedelta

from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from lexwatch.models import Bill, BillEvent, Match, Profile, Rule, RuleScore, Run, User
from lexwatch.scoring.fake import FakeScorer
from lexwatch.scoring.text import bill_text
from tests.conftest import alembic_config

BILLS = [
    (
        "8410",
        "projet",
        "in_committee",
        "Projet de loi relative à la lutte contre le blanchiment de capitaux et contre le financement du terrorisme",
    ),
    (
        "8409",
        "projet",
        "deposited",
        "Projet de loi portant modification de la loi modifiée du 5 avril 1993 relative au secteur financier",
    ),
    (
        "8408",
        "proposition",
        "deposited",
        "Proposition de loi portant modification du Code de la route",
    ),
    (
        "8406",
        "projet",
        "council_opinion",
        "Projet de loi relative aux marchés de crypto-actifs et portant modification de diverses lois financières",
    ),
    (
        "8401",
        "projet",
        "second_vote_pending",
        "Projet de loi portant création d'un registre des bénéficiaires effectifs des fiducies",
    ),
    (
        "8399",
        "projet",
        "adopted",
        "Projet de loi relatif à l'aménagement du territoire et aux plans directeurs sectoriels",
    ),
    (
        "8395",
        "proposition",
        "withdrawn",
        "Proposition de loi visant à renforcer la protection des lanceurs d'alerte",
    ),
    (
        "8390",
        "projet",
        "published",
        "Projet de loi portant transposition de la directive (UE) 2024/1640 relative aux mécanismes de prévention du blanchiment de capitaux",
    ),
    (
        "8385",
        "projet",
        "in_committee",
        "Projet de loi relative à l'organisation de la sécurité civile",
    ),
    (
        "8380",
        "projet",
        "rejected",
        "Projet de loi portant réforme de l'impôt sur le revenu des collectivités",
    ),
    (
        "8377",
        "projet",
        "merged",
        "Projet de loi relatif aux services de paiement et à la monnaie électronique",
    ),
    ("8370", "proposition", "other", "Proposition de loi relative au logement abordable"),
]
for i in range(48):
    BILLS.append(
        (
            str(8300 - i),
            "projet",
            "deposited",
            f"Projet de loi portant approbation de l'accord n° {i + 1} en matière fiscale",
        )
    )

PROFILES = [
    (
        "admin",
        "Lutte anti-blanchiment",
        None,
        [
            "Lutte contre le blanchiment de capitaux et le financement du terrorisme",
            "Secteur financier, services de paiement et crypto-actifs",
            "Bénéficiaires effectifs et transparence",
        ],
        "conformite@example.org\njuridique@example.org",
    ),
    ("admin", "Aménagement du territoire", 0.4, ["Aménagement du territoire et logement"], ""),
    ("user", "Mobilité", None, ["Code de la route et mobilité"], "mobilite@example.org"),
]


def seed(url: str) -> None:
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    command.upgrade(alembic_config(url), "head")
    now = datetime.now(UTC)
    scorer = FakeScorer()
    with Session(engine) as db:
        users = {
            "admin": User(
                oidc_sub="dev-admin",
                email="admin@lexwatch.local",
                name="Dev Admin",
                role="admin",
                language="fr",
                previous_visit_at=now - timedelta(days=2),
                last_visit_at=now - timedelta(days=2),
            ),
            "user": User(
                oidc_sub="dev-user",
                email="user@lexwatch.local",
                name="Dev User",
                role="user",
                language="fr",
                previous_visit_at=now - timedelta(days=2),
                last_visit_at=now - timedelta(days=2),
            ),
        }
        db.add_all(users.values())
        db.flush()
        bills = []
        for i, (number, kind, status, title) in enumerate(BILLS):
            changed = now - timedelta(days=1) if i in (0, 3, 8) else now - timedelta(days=30 + i)
            bill = Bill(
                number=number,
                type=kind,
                status=status,
                title=title,
                filing_date=date(2025, 11, 1) - timedelta(days=9 * i),
                author="Ministère des Finances" if kind == "projet" else "Groupe parlementaire",
                summary=None,
                source="datapublic-112",
                score_state="scored",
                first_seen_at=changed,
                last_changed_at=changed,
            )
            bills.append(bill)
        db.add_all(bills)
        bills[-1].score_state = "unscored"
        db.flush()
        db.add_all(
            [
                BillEvent(
                    bill_number="8410",
                    field="status",
                    old_value="deposited",
                    new_value="in_committee",
                    source="chd",
                    changed_at=now - timedelta(days=1),
                ),
                BillEvent(
                    bill_number="8410",
                    field="title",
                    old_value="Projet de loi relative au blanchiment",
                    new_value=BILLS[0][3],
                    source="datapublic-112",
                    changed_at=now - timedelta(days=20),
                ),
            ]
        )
        for owner, name, threshold, rules, recipients in PROFILES:
            profile = Profile(
                name=name, owner_id=users[owner].id, threshold=threshold, recipients=recipients
            )
            profile.rules = [Rule(text=r, position=i) for i, r in enumerate(rules)]
            db.add(profile)
            db.flush()
            texts = [bill_text(b) for b in bills]
            per_rule = []
            for rule in profile.rules:
                scores = scorer.score_many(rule.text, texts)
                per_rule.append(scores)
                db.add_all(
                    RuleScore(rule_id=rule.id, bill_number=b.number, score=s, model="fake")
                    for b, s in zip(bills, scores, strict=True)
                )
            for j, bill in enumerate(bills):
                mean = sum(s[j] for s in per_rule) / len(per_rule)
                db.add(
                    Match(
                        profile_id=profile.id,
                        bill_number=bill.number,
                        profile_score=mean,
                        matched=mean >= (threshold if threshold is not None else 0.2),
                    )
                )
        db.query(Profile).filter(Profile.name == "Aménagement du territoire").update(
            {"rescore_status": "running"}
        )
        db.add_all(
            [
                Run(
                    kind="daily",
                    status="success",
                    created_at=now - timedelta(hours=5),
                    started_at=now - timedelta(hours=5),
                    finished_at=now - timedelta(hours=5) + timedelta(seconds=94),
                    bills_added=3,
                    bills_changed=5,
                    bills_scored=8,
                    billed_units=4210,
                    details={
                        "sources": {
                            "datapublic-112": {"status": "ok", "records": 812},
                            "datapublic-121": {"status": "ok", "records": 640},
                            "chd": {
                                "status": "failed",
                                "records": 0,
                                "error": "HTTP 503 from www.chd.lu",
                            },
                        },
                        "billing": {"unit": "input_tokens"},
                    },
                ),
                Run(
                    kind="daily",
                    status="partial",
                    created_at=now - timedelta(days=1, hours=5),
                    started_at=now - timedelta(days=1, hours=5),
                    finished_at=now - timedelta(days=1, hours=5) + timedelta(minutes=3, seconds=12),
                    bills_added=0,
                    bills_changed=2,
                    bills_scored=1,
                    bills_unscored=1,
                    billed_units=950,
                    error_message="scoring: ScorerUnavailable('circuit open after 5 failures')",
                    details={"billing": {"unit": "input_tokens"}},
                ),
                Run(
                    kind="manual",
                    status="queued",
                    created_at=now - timedelta(minutes=1),
                    requested_by=users["admin"].id,
                ),
            ]
        )
        db.commit()
    print(f"seeded {len(BILLS)} bills, {len(PROFILES)} profiles")


if __name__ == "__main__":
    seed(sys.argv[1])
