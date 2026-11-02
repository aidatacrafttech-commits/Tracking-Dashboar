"""A minimal in-process SMTP server for tests (localhost only, nothing is relayed).

Speaks enough ESMTP for smtplib: EHLO/HELO, AUTH PLAIN, MAIL, RCPT, DATA, RSET, NOOP,
QUIT. ``mode`` simulates failures: "ok", "tempfail" (451 on MAIL), "reject_rcpt" (550),
"auth_fail" (535).
"""

from __future__ import annotations

import base64
import socketserver
import threading
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import Message


@dataclass
class Received:
    mail_from: str
    rcpt_to: list[str]
    raw: bytes

    @property
    def message(self) -> Message:
        return message_from_bytes(self.raw, policy=policy.default)


@dataclass
class SinkState:
    mode: str = "ok"
    messages: list[Received] = field(default_factory=list)
    auth_seen: list[tuple[str, str]] = field(default_factory=list)


class _Handler(socketserver.StreamRequestHandler):
    state: SinkState

    def reply(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())

    def handle(self) -> None:
        state = self.server.state  # type: ignore[attr-defined]
        self.reply("220 sink.test ESMTP")
        mail_from, rcpts = "", []
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            cmd = line[:4].upper()
            if cmd in ("EHLO", "HELO"):
                self.reply("250-sink.test")
                self.reply("250 AUTH PLAIN")
            elif cmd == "AUTH":
                parts = line.split()
                decoded = base64.b64decode(parts[2]).split(b"\0") if len(parts) > 2 else []
                if len(decoded) == 3:
                    state.auth_seen.append((decoded[1].decode(), decoded[2].decode()))
                self.reply("535 bad credentials" if state.mode == "auth_fail" else "235 ok")
            elif cmd == "MAIL":
                if state.mode == "tempfail":
                    self.reply("451 try again later")
                    continue
                mail_from, rcpts = line.split(":", 1)[1].strip(" <>"), []
                self.reply("250 ok")
            elif cmd == "RCPT":
                if state.mode == "reject_rcpt":
                    self.reply("550 no such user")
                    continue
                rcpts.append(line.split(":", 1)[1].strip(" <>"))
                self.reply("250 ok")
            elif cmd == "DATA":
                self.reply("354 end with .")
                chunks = []
                while True:
                    data = self.rfile.readline()
                    if data in (b".\r\n", b".\n", b""):
                        break
                    chunks.append(data[1:] if data.startswith(b"..") else data)
                state.messages.append(Received(mail_from, rcpts, b"".join(chunks)))
                self.reply("250 queued")
            elif cmd == "QUIT":
                self.reply("221 bye")
                return
            else:  # RSET, NOOP, ...
                self.reply("250 ok")


class SmtpSink:
    def __init__(self) -> None:
        self.state = SinkState()
        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.state = self.state  # type: ignore[attr-defined]
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
        )

    def __enter__(self) -> SmtpSink:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()

    @property
    def messages(self) -> list[Received]:
        return self.state.messages
