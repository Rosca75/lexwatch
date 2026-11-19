"""No client IP address in the logs (spec section 8): uvicorn access log off, own access line."""

from __future__ import annotations

import logging

from lexwatch import cli
from lexwatch.logging import DropClientAddress, configure_logging


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


def test_uvicorn_access_records_lose_the_client_address():
    configure_logging("INFO")
    access = logging.getLogger("uvicorn.access")
    assert any(isinstance(f, DropClientAddress) for f in access.filters)
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.9:5000", "GET", "/", "1.1", 200),
        None,
    )
    access.filters[0].filter(record)
    assert "203.0.113.9" not in record.getMessage()


def test_serve_disables_uvicorn_access_log(monkeypatch, settings):
    import uvicorn

    seen = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: seen.update(k))
    cli.cmd_serve(settings, cli.build_parser().parse_args(["serve"]))
    assert seen["access_log"] is False
