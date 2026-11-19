"""Daily email digest per profile (stream 4).

One email per profile per day, only when something is new or changed. A bill is in the digest
of a profile when its match is ``matched`` and, within the window ``(since, now]``:

- the bill was first seen (``bills.first_seen_at``) or last changed (``bills.last_changed_at``),
- or its match was (re)computed (``matches.computed_at``), i.e. it newly matches the profile,
  for example after new rules or a lower threshold.

``since`` is ``profile.last_digest_at``. For a profile that never got a digest
(``last_digest_at IS NULL``) the window is the last 24 hours and only bill changes count, not
match computations: otherwise the first digest after creating a profile would list every bill
it matches (they are all in the app already). ``last_digest_at`` is set to ``now`` only after a
successful send, so nothing is lost when a send fails or no digest was due.

Rendered from ``web/templates/email/digest.html`` and ``digest.txt`` in the profile owner's
language, with absolute links built from ``settings.base_url``. Recipients come from
``profiles.recipients`` (commas, semicolons or newlines); invalid addresses are skipped.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session, joinedload

from lexwatch.config import Settings, get_settings
from lexwatch.i18n import normalize_lang, t
from lexwatch.models import Bill, Match, Profile
from lexwatch.notify import mailer

log = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "web" / "templates"
FIRST_DIGEST_WINDOW = timedelta(hours=24)
SOURCE_URL = "https://www.chd.lu"
# App paths linked from the email (stream 3 routes).
BILL_PATH = "/bills/{number}"
PROFILE_PATH = "/?profile={id}"

_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_EMAIL_RE = re.compile(rf"^[^@\s<>(),;:\"\[\]]+@{_LABEL}(?:\.{_LABEL})+$")
_SPLIT_RE = re.compile(r"[,;\n\r]+")


@dataclass(slots=True)
class Digest:
    profile_id: int
    recipients: list[str]
    subject: str
    html: str
    text: str
    bill_numbers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DigestReport:
    """Outcome of one ``send_digests_report`` call."""

    sent: int = 0
    skipped: int = 0  # nothing new, or no valid recipient
    failed: dict[int, str] = field(default_factory=dict)  # profile id -> error
    digests: list[Digest] = field(default_factory=list)  # built digests (sent or dry run)


class DigestError(Exception):
    """Some digests could not be sent. Successful sends are already committed."""

    def __init__(self, report: DigestReport) -> None:
        self.report = report
        details = "; ".join(f"profile {pid}: {err}" for pid, err in report.failed.items())
        super().__init__(f"{len(report.failed)} digest(s) failed, {report.sent} sent: {details}")


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_recipients(raw: str | None) -> tuple[list[str], list[str]]:
    """Split ``raw`` on commas, semicolons and newlines. Return ``(valid, invalid)``.

    ``Name <a@b.lu>`` is accepted and reduced to the address. Duplicates are removed
    (case-insensitive), the first spelling wins.
    """
    valid: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for part in _SPLIT_RE.split(raw or ""):
        item = part.strip()
        if not item:
            continue
        match = re.fullmatch(r".*<([^<>]+)>", item)
        address = (match.group(1) if match else item).strip()
        if len(address) > 254 or not _EMAIL_RE.match(address):
            invalid.append(item)
            continue
        if address.casefold() not in seen:
            seen.add(address.casefold())
            valid.append(address)
    return valid, invalid


_env: Environment | None = None


def template_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(enabled_extensions=("html",), default=False),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
    return _env


def status_label(status: str | None, lang: str) -> str:
    if not status:
        return ""
    key = f"bill.status.{status}"
    label = t(key, lang)
    return status if label == key else label


def window_start(since: datetime | None, now: datetime) -> datetime:
    return since if since is not None else now - FIRST_DIGEST_WINDOW


def digest_rows(
    session: Session, profile: Profile, since: datetime | None, now: datetime
) -> list[tuple[Bill, Match]]:
    """Matched bills of ``profile`` that are new or changed in ``(since, now]``."""
    start = window_start(since, now)

    def in_window(column: Any) -> Any:
        return and_(column > start, column <= now)

    changed = [in_window(Bill.first_seen_at), in_window(Bill.last_changed_at)]
    if since is not None:
        changed.append(in_window(Match.computed_at))
    stmt = (
        select(Bill, Match)
        .join(Match, Match.bill_number == Bill.number)
        .where(Match.profile_id == profile.id, Match.matched.is_(True), or_(*changed))
        .order_by(Bill.last_changed_at.desc(), Bill.number.desc())
    )
    return [(bill, match) for bill, match in session.execute(stmt).all()]


def build_digest(
    session: Session,
    profile: Profile,
    since: datetime | None,
    lang: str = "fr",
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> Digest | None:
    """Return the digest for ``profile`` or ``None`` when there is nothing new or changed."""
    settings = settings or get_settings()
    now = now or utcnow()
    lang = normalize_lang(lang)
    rows = digest_rows(session, profile, since, now)
    if not rows:
        return None
    base = settings.base_url.rstrip("/")
    tr = partial(t, lang=lang)
    bills: list[dict[str, Any]] = [
        {
            "number": bill.number,
            "title": bill.title or "",
            "status": status_label(bill.status, lang),
            "score": f"{match.profile_score:.2f}",
            "url": base + BILL_PATH.format(number=bill.number),
            "is_new": bill.first_seen_at > window_start(since, now),
        }
        for bill, match in rows
    ]
    subject = tr("email.digest.subject", profile=profile.name, count=len(bills))
    context = {
        "lang": lang,
        "t": tr,
        "subject": subject,
        "profile_name": profile.name,
        "profile_url": base + PROFILE_PATH.format(id=profile.id),
        "app_url": base + "/",
        "bills": bills,
        "count": len(bills),
        "source_url": SOURCE_URL,
    }
    env = template_env()
    html = env.get_template("email/digest.html").render(context)
    text = env.get_template("email/digest.txt").render(context)
    recipients, _ = parse_recipients(profile.recipients)
    return Digest(
        profile_id=profile.id,
        recipients=recipients,
        subject=subject,
        html=html,
        text=text,
        bill_numbers=[b["number"] for b in bills],
    )


def _profile_lang(profile: Profile) -> str:
    owner = profile.owner
    return normalize_lang(owner.language if owner is not None else None)


def send_digests_report(
    session: Session,
    now: datetime | None = None,
    dry_run: bool = False,
    *,
    settings: Settings | None = None,
    echo: Any = None,
) -> DigestReport:
    """Build and send the digest of every profile. Never raises for a single profile.

    After each successful send, ``profile.last_digest_at = now`` is committed at once: the mail
    is out, so the timestamp must survive a later failure (this is the one service function
    that commits). ``dry_run`` renders the digests, prints them with ``echo`` (default
    ``print``) and changes nothing.
    """
    settings = settings or get_settings()
    now = now or utcnow()
    report = DigestReport()
    if not dry_run and not settings.smtp_host:
        log.warning("digest.smtp_not_configured")
        return report
    echo = echo or print
    profiles = session.scalars(
        select(Profile).options(joinedload(Profile.owner)).order_by(Profile.id)
    ).all()
    for profile in profiles:
        recipients, invalid = parse_recipients(profile.recipients)
        if invalid:
            log.warning(
                "digest.invalid_recipients",
                extra={"profile_id": profile.id, "count": len(invalid)},
            )
        if not recipients:
            report.skipped += 1
            continue
        try:
            digest = build_digest(
                session,
                profile,
                profile.last_digest_at,
                _profile_lang(profile),
                settings=settings,
                now=now,
            )
        except Exception as exc:
            log.exception("digest.build_failed", extra={"profile_id": profile.id})
            report.failed[profile.id] = f"build: {type(exc).__name__}: {exc}"
            continue
        if digest is None:
            report.skipped += 1
            continue
        report.digests.append(digest)
        if dry_run:
            echo(_dry_run_text(digest))
            report.sent += 1
            continue
        message = mailer.build_message(
            settings, digest.recipients, digest.subject, digest.html, digest.text
        )
        try:
            mailer.send_message(settings, message)
        except mailer.MailerError as exc:
            log.error("digest.send_failed", extra={"profile_id": profile.id, "error": str(exc)})
            report.failed[profile.id] = str(exc)
            continue
        profile.last_digest_at = now
        session.commit()
        report.sent += 1
        log.info(
            "digest.sent",
            extra={
                "profile_id": profile.id,
                "bills": len(digest.bill_numbers),
                "recipients": len(digest.recipients),
            },
        )
    return report


def _dry_run_text(digest: Digest) -> str:
    return (
        f"--- profile {digest.profile_id} ---\n"
        f"To: {', '.join(digest.recipients)}\n"
        f"Subject: {digest.subject}\n\n"
        f"{digest.text}"
    )


def send_digests(
    session: Session,
    now: datetime | None = None,
    dry_run: bool = False,
    *,
    settings: Settings | None = None,
) -> int:
    """Build and send digests for all profiles; update ``last_digest_at``. Return count sent.

    Profiles that failed do not stop the others; when any failed, ``DigestError`` is raised at
    the end (its ``report`` has the counts and per-profile errors). Successful sends are
    already committed by then.
    """
    report = send_digests_report(session, now=now, dry_run=dry_run, settings=settings)
    if report.failed:
        raise DigestError(report)
    return report.sent
