"""SMTP sending with the standard library (stream 4).

``SMTP_SSL=true`` uses implicit TLS (usually port 465). Otherwise a plain connection is opened
and upgraded with STARTTLS when ``SMTP_STARTTLS=true`` (usually port 587). Login happens only
when ``SMTP_USERNAME`` is set. Every connection uses ``SMTP_TIMEOUT_SECONDS``.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from lexwatch.config import Settings

log = logging.getLogger(__name__)


class MailerError(Exception):
    """Sending failed (connection, TLS, authentication or refused recipients)."""


class MailerNotConfigured(MailerError):
    """``SMTP_HOST`` is empty."""


def _msgid_domain(sender: str) -> str | None:
    address = sender.rsplit("<", 1)[-1].rstrip(">").strip()
    domain = address.rpartition("@")[2]
    return domain or None


def build_message(
    settings: Settings, to: list[str], subject: str, html: str, text: str
) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = ", ".join(to)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False, usegmt=True)
    msg["Message-ID"] = make_msgid(domain=_msgid_domain(settings.smtp_from))
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    return msg


def _connect(settings: Settings) -> smtplib.SMTP:
    context = ssl.create_default_context()
    timeout = settings.smtp_timeout_seconds
    if settings.smtp_ssl:
        return smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=timeout, context=context
        )
    smtp = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=timeout)
    try:
        if settings.smtp_starttls:
            smtp.starttls(context=context)
    except BaseException:
        smtp.close()
        raise
    return smtp


def send_message(settings: Settings, message: EmailMessage) -> None:
    """Send via ``smtplib`` (SMTP_SSL or SMTP + STARTTLS, optional login).

    Raises ``MailerError`` on any failure. When the relay refuses only some recipients the
    message still counts as sent (the others got it); the refusals are logged.
    """
    if not settings.smtp_host:
        raise MailerNotConfigured("SMTP_HOST is not set")
    try:
        with _connect(settings) as smtp:
            if settings.smtp_username:
                smtp.login(settings.smtp_username, settings.smtp_password)
            refused = smtp.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        raise MailerError(f"{type(exc).__name__}: {exc}") from exc
    if refused:
        log.warning("mail.recipients_refused", extra={"refused_count": len(refused)})
    log.info("mail.sent", extra={"message_id": message["Message-ID"]})
