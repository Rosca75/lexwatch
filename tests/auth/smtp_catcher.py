"""A tiny in-process SMTP server for tests (no STARTTLS; AUTH PLAIN accepted)."""

from __future__ import annotations

import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage


@dataclass
class Received:
    mail_from: str
    rcpt_to: list[str]
    data: bytes
    auth: str | None = None

    @property
    def message(self) -> EmailMessage:
        return message_from_bytes(self.data, policy=policy.default)  # type: ignore[return-value]


@dataclass
class CatcherState:
    messages: list[Received] = field(default_factory=list)
    refuse: set[str] = field(default_factory=set)


class _Handler(socketserver.StreamRequestHandler):
    server: _Server

    def _send(self, line: str) -> None:
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self) -> None:
        state = self.server.state
        self._send("220 catcher ESMTP")
        mail_from, rcpts, auth = "", [], None
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            cmd = line.upper()
            if cmd.startswith("EHLO"):
                self._send("250-catcher")
                self._send("250-AUTH PLAIN")
                self._send("250 8BITMIME")
            elif cmd.startswith("HELO"):
                self._send("250 catcher")
            elif cmd.startswith("AUTH PLAIN"):
                auth = line[len("AUTH PLAIN ") :]
                self._send("235 2.7.0 Authentication successful")
            elif cmd.startswith("MAIL FROM:"):
                mail_from = line[10:].split()[0].strip("<>")
                rcpts = []
                self._send("250 OK")
            elif cmd.startswith("RCPT TO:"):
                address = line[8:].split()[0].strip("<>")
                if address in state.refuse:
                    self._send("550 5.1.1 refused")
                else:
                    rcpts.append(address)
                    self._send("250 OK")
            elif cmd == "DATA":
                self._send("354 go ahead")
                chunks: list[bytes] = []
                while True:
                    data_line = self.rfile.readline()
                    if data_line in (b".\r\n", b".\n", b""):
                        break
                    if data_line.startswith(b".."):
                        data_line = data_line[1:]
                    chunks.append(data_line)
                state.messages.append(Received(mail_from, rcpts, b"".join(chunks), auth))
                self._send("250 OK queued")
            elif cmd in ("RSET", "NOOP"):
                self._send("250 OK")
            elif cmd == "QUIT":
                self._send("221 bye")
                return
            else:
                self._send("502 not implemented")


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
    state: CatcherState


@contextmanager
def smtp_catcher() -> Iterator[tuple[int, CatcherState]]:
    """Yield ``(port, state)`` of a running catcher on 127.0.0.1."""
    server = _Server(("127.0.0.1", 0), _Handler)
    server.state = CatcherState()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], server.state
    finally:
        server.shutdown()
        server.server_close()
