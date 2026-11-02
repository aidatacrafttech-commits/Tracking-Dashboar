#!/usr/bin/env python3
"""Check outbound access from this host to every endpoint the monitor needs.

DNS lookup + TCP connect + TLS handshake only. No request is sent, so no credentials are
used and nothing is billed. Standard library only.

    python3 check_outbound.py [--smtp-host smtp.example.com --smtp-port 587] [--aws-region ap-south-1]
"""

from __future__ import annotations

import argparse
import socket
import ssl
import sys

HTTPS = [
    ("OpenAI API", "api.openai.com"),
    ("OpenAI status", "status.openai.com"),
    ("Deepgram API", "api.deepgram.com"),
    ("Deepgram status", "status.deepgram.com"),
    ("ElevenLabs API", "api.elevenlabs.io"),
    ("ElevenLabs status", "status.elevenlabs.io"),
    ("Sarvam status", "status.sarvam.ai"),
    ("AWS Cost Explorer", "ce.us-east-1.amazonaws.com"),
    ("AWS Billing (credits)", "billing.us-east-1.api.aws"),
    ("AWS STS", "sts.us-east-1.amazonaws.com"),
    ("Python packages (deploy time)", "pypi.org"),
    ("Python packages (deploy time)", "files.pythonhosted.org"),
]


def probe(host: str, port: int, tls: bool, starttls: bool = False) -> str:
    addr = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4]
    with socket.create_connection((host, port), timeout=8) as sock:
        if starttls:
            banner = sock.recv(512)
            if not banner.startswith(b"220"):
                return f"unexpected banner {banner[:40]!r}"
            sock.sendall(b"EHLO monitor-check\r\n")
            sock.recv(2048)
            sock.sendall(b"STARTTLS\r\n")
            if not sock.recv(512).startswith(b"220"):
                return "server refused STARTTLS"
        if tls or starttls:
            with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls_sock:
                return f"ok ({addr[0]}, {tls_sock.version()})"
        return f"ok ({addr[0]})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--smtp-host")
    parser.add_argument("--smtp-port", type=int, default=587)
    parser.add_argument("--aws-region", action="append", default=[])
    args = parser.parse_args()

    targets = [(name, host, 443, True, False) for name, host in HTTPS]
    for region in args.aws_region or ["ap-south-1"]:
        targets += [
            (f"AWS EC2 ({region})", f"ec2.{region}.amazonaws.com", 443, True, False),
            (f"AWS CloudWatch ({region})", f"monitoring.{region}.amazonaws.com", 443, True, False),
        ]
    if args.smtp_host:
        port = args.smtp_port
        targets.append((f"SMTP {port}", args.smtp_host, port, port == 465, port == 587))

    failures = 0
    for name, host, port, tls, starttls in targets:
        try:
            result = probe(host, port, tls, starttls)
        except (OSError, ssl.SSLError) as exc:
            result = f"FAILED: {type(exc).__name__}: {exc}"
        ok = result.startswith("ok")
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {name:<32} {host}:{port}  {result}")
    print(f"{failures} endpoint(s) unreachable" if failures else "all endpoints reachable")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
