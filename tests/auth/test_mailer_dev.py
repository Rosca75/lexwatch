"""Mailer: message headers, STARTTLS / implicit TLS / login paths (smtplib mocked)."""

from __future__ import annotations

import smtplib

import pytest

from lexwatch.notify import mailer
from tests.helpers import make_test_settings


class FakeSMTP:
    instances: list[FakeSMTP] = []

    def __init__(self, host, port, timeout=None, context=None):
        self.host, self.port, self.timeout, self.context = host, port, timeout, context
        self.calls: list[str] = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.calls.append("quit")
        return False

    def starttls(self, context=None):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}")

    def send_message(self, message):
        self.calls.append("send")
        return {}

    def close(self):
        self.closed = True


class FakeSMTPSSL(FakeSMTP):
    pass


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", FakeSMTPSSL)


def _msg(settings):
    return mailer.build_message(settings, ["a@x.lu", "b@x.lu"], "Sujet é", "<p>h</p>", "t")


def test_build_message_headers():
    settings = make_test_settings(smtp_from="lexwatch <noreply@example.lu>")
    msg = _msg(settings)
    assert msg["From"] == "lexwatch <noreply@example.lu>"
    assert msg["To"] == "a@x.lu, b@x.lu"
    assert msg["Subject"] == "Sujet é"
    assert msg["Message-ID"].endswith("@example.lu>")
    assert msg["Date"]
    assert msg["Auto-Submitted"] == "auto-generated"
    assert [p.get_content_type() for p in msg.iter_parts()] == ["text/plain", "text/html"]


def test_starttls_and_login():
    settings = make_test_settings(
        smtp_host="relay",
        smtp_port=587,
        smtp_username="u",
        smtp_password="p",
        smtp_timeout_seconds=7,
    )
    mailer.send_message(settings, _msg(settings))
    (smtp,) = FakeSMTP.instances
    assert type(smtp) is FakeSMTP
    assert (smtp.host, smtp.port, smtp.timeout) == ("relay", 587, 7)
    assert smtp.calls == ["starttls", "login:u", "send", "quit"]


def test_implicit_tls_without_login():
    settings = make_test_settings(smtp_host="relay", smtp_port=465, smtp_ssl=True)
    mailer.send_message(settings, _msg(settings))
    (smtp,) = FakeSMTP.instances
    assert type(smtp) is FakeSMTPSSL
    assert smtp.context is not None
    assert smtp.calls == ["send", "quit"]


def test_plain_without_starttls():
    settings = make_test_settings(smtp_host="relay", smtp_port=25, smtp_starttls=False)
    mailer.send_message(settings, _msg(settings))
    assert FakeSMTP.instances[0].calls == ["send", "quit"]


def test_not_configured():
    with pytest.raises(mailer.MailerNotConfigured):
        mailer.send_message(make_test_settings(smtp_host=""), _msg(make_test_settings()))


def test_smtp_errors_become_mailer_error(monkeypatch):
    def boom(self, user, password):
        raise smtplib.SMTPAuthenticationError(535, b"bad credentials")

    monkeypatch.setattr(FakeSMTP, "login", boom)
    settings = make_test_settings(smtp_host="relay", smtp_username="u")
    with pytest.raises(mailer.MailerError, match="SMTPAuthenticationError"):
        mailer.send_message(settings, _msg(settings))


def test_starttls_failure_closes_connection(monkeypatch):
    def fail(self, context=None):
        raise smtplib.SMTPNotSupportedError("no STARTTLS")

    monkeypatch.setattr(FakeSMTP, "starttls", fail)
    settings = make_test_settings(smtp_host="relay")
    with pytest.raises(mailer.MailerError):
        mailer.send_message(settings, _msg(settings))
    assert FakeSMTP.instances[0].closed
