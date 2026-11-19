"""Small builders for web tests (and the dev seed script)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import count

from sqlalchemy import select
from sqlalchemy.orm import Session

from lexwatch.models import Bill, BillEvent, Match, Profile, Rule, RuleScore, User
from lexwatch.web.deps import get_or_create_dev_user

_numbers = count(7000)


def dev_user(db: Session, role: str) -> User:
    return get_or_create_dev_user(db, role)


def other_user(db: Session, sub: str = "someone-else") -> User:
    user = db.scalar(select(User).where(User.oidc_sub == sub))
    if user is None:
        user = User(oidc_sub=sub, email=f"{sub}@example.org", name=sub, role="user")
        db.add(user)
        db.flush()
    return user


def make_bill(db: Session, number: str | None = None, **fields: object) -> Bill:
    values: dict[str, object] = {
        "number": number or str(next(_numbers)),
        "type": "projet",
        "title": "Projet de loi portant modification de diverses lois",
        "status": "deposited",
        "filing_date": date(2025, 1, 15),
        "source": "datapublic-112",
        "score_state": "scored",
        "last_changed_at": datetime(2025, 1, 15, tzinfo=UTC),
    }
    values.update(fields)
    bill = Bill(**values)
    db.add(bill)
    db.flush()
    return bill


def make_profile(
    db: Session,
    owner: User,
    name: str = "AML",
    rules: tuple[str, ...] = ("blanchiment de capitaux",),
    threshold: float | None = None,
    recipients: str = "",
) -> Profile:
    profile = Profile(name=name, owner_id=owner.id, threshold=threshold, recipients=recipients)
    profile.rules = [Rule(text=text, position=i) for i, text in enumerate(rules)]
    db.add(profile)
    db.flush()
    return profile


def make_match(
    db: Session, profile: Profile, bill: Bill, score: float, matched: bool | None = None
) -> Match:
    match = Match(
        profile_id=profile.id,
        bill_number=bill.number,
        profile_score=score,
        matched=score >= 0.5 if matched is None else matched,
    )
    db.add(match)
    for rule in profile.rules:
        db.add(RuleScore(rule_id=rule.id, bill_number=bill.number, score=score, model="fake"))
    db.flush()
    return match


def make_event(db: Session, bill: Bill, field: str, old: str | None, new: str | None) -> BillEvent:
    event = BillEvent(
        bill_number=bill.number,
        field=field,
        old_value=old,
        new_value=new,
        source="chd",
        changed_at=datetime.now(UTC) - timedelta(days=1),
    )
    db.add(event)
    db.flush()
    return event
