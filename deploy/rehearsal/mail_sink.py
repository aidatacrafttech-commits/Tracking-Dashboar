#!/usr/bin/env python3
"""Local SMTP sink for the deployment rehearsal: listens on 127.0.0.1:PORT, relays nothing,
and writes every message it receives to MAILDIR as a .eml file.

    python3 mail_sink.py 2525 ~/acm-rehearsal/mail
"""

from __future__ import annotations

import socketserver
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from tests.smtp_sink import SinkState, _Handler  # noqa: E402


def main() -> None:
    port, maildir = int(sys.argv[1]), Path(sys.argv[2]).expanduser()
    maildir.mkdir(parents=True, exist_ok=True)
    server = socketserver.ThreadingTCPServer(("127.0.0.1", port), _Handler)
    server.daemon_threads = True
    server.state = SinkState()  # type: ignore[attr-defined]
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"mail sink on 127.0.0.1:{port} -> {maildir}", flush=True)
    written = 0
    while True:
        messages = server.state.messages  # type: ignore[attr-defined]
        while written < len(messages):
            msg = messages[written]
            written += 1
            (maildir / f"{written:04d}.eml").write_bytes(msg.raw)
            print(f"received #{written}: to={msg.rcpt_to} subject={msg.message['Subject']}", flush=True)
        time.sleep(0.2)


if __name__ == "__main__":
    main()
