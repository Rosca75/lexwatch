"""No client IP address in the logs (spec section 8): uvicorn access log off, own access line."""

from __future__ import annotations

import logging

from lexwatch import cli
from lexwatch.logging import configure_logging


def test_access_line_has_method_path_status_and_user_id(client, caplog):
    with caplog.at_level(logging.INFO, logger="lexwatch.access"):
        client.get("/?q=secret+search", headers={"X-Forwarded-For": "203.0.113.9"})
    records = [r for r in caplog.records if r.getMessage() == "http.request"]
    assert records, "no http.request line"
    record = records[-1]
    assert (record.method, record.path, record.status) == ("GET", "/", 200)
    assert isinstance(record.user_id, int)
    assert "203.0.113.9" not in caplog.text and "testclient" not in caplog.text
    assert "secret" not in caplog.text  # no query string


def test_health_and_static_are_not_logged(client, caplog):
    with caplog.at_level(logging.INFO, logger="lexwatch.access"):
        client.get("/health")
        client.get("/static/style.css")
    assert not [r for r in caplog.records if r.getMessage() == "http.request"]


def test_uvicorn_access_log_is_silenced():
    configure_logging("INFO")
    access = logging.getLogger("uvicorn.access")
    # uvicorn's protocols only write access lines when the logger has handlers.
    assert access.hasHandlers() is False


def test_serve_disables_uvicorn_access_log(monkeypatch, settings):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.update(k))
    cli.cmd_serve(settings, cli.build_parser().parse_args(["serve"]))
    assert seen["access_log"] is False
