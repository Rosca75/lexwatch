"""Daily email digest per profile (stream 4).

One email per profile per day, only when bills matching the profile are new or changed since
``since`` (normally ``profile.last_digest_at``). Rendered from
``web/templates/email/digest.html`` and ``digest.txt`` in the recipients' language (default fr),
with absolute links built from ``settings.base_url``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from lexwatch.models import Profile


@dataclass(slots=True)
class Digest:
    profile_id: int
    recipients: list[str]
    subject: str
    html: str
    text: str
    bill_numbers: list[str] = field(default_factory=list)


def build_digest(
    session: Session, profile: Profile, since: datetime | None, lang: str = "fr"
) -> Digest | None:
    """Return the digest for ``profile`` or ``None`` when there is nothing new or changed."""
    raise NotImplementedError("stream 4: build digest")


def send_digests(session: Session, now: datetime | None = None, dry_run: bool = False) -> int:
    """Build and send digests for all profiles; update ``last_digest_at``. Return count sent."""
    raise NotImplementedError("stream 4: send digests")
