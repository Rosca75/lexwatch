"""SMTP sending with the standard library (stream 4)."""

from __future__ import annotations

from email.message import EmailMessage

from lexwatch.config import Settings


def build_message(
    settings: Settings, to: list[str], subject: str, html: str, text: str
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def send_message(settings: Settings, message: EmailMessage) -> None:
    """Send via ``smtplib`` (SMTP_SSL or SMTP + STARTTLS, optional login)."""
    raise NotImplementedError("stream 4: SMTP send")
