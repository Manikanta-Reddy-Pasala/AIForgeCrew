#!/usr/bin/env python3
"""Say why a TLS connection to the package index fails, in the operator's terms.

pip reports a certificate it cannot verify as "No matching distribution found
for uv==0.12.11" — it asked the index for versions, got nothing back, and told
you about the symptom. On a corporate box that is nearly always a private or
inspecting CA, and the fix is not a different version. run.sh calls this on the
failure path so the message names the real cause.

Usage: tls_probe.py HOST [PORT]
"""
from __future__ import annotations

import socket
import ssl
import sys


def _verify_failed(host: str, port: int, exc: ssl.SSLCertVerificationError) -> None:
    print(f"==> CA: {host} presents a certificate this machine does not trust.")
    print(f"    {getattr(exc, 'verify_message', None) or exc}")
    print("    The index sits behind a private (or TLS-inspecting) CA. Install"
          " its root:")
    print(f"      openssl s_client -showcerts -connect {host}:{port} </dev/null"
          " 2>/dev/null \\")
    print("        | awk '/BEGIN CERT/,/END CERT/' > /tmp/ca.pem")
    print("      mkdir -p ~/.aiforge/security/ca")
    print("      cp /tmp/ca.pem ~/.aiforge/security/ca/custom-ca.pem")
    print("    …then re-run. One-shot instead: AIFORGE_CA_BUNDLE=/tmp/ca.pem"
          " ./run.sh")
    print("    If openssl DOES verify the host and only Python does not, the")
    print("    root is in the OS store and Python cannot see it — install"
          " truststore.")


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: tls_probe.py HOST [PORT]", file=sys.stderr)
        return 2
    host = argv[0]
    port = int(argv[1]) if len(argv) > 1 else 443
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=8) as raw, \
                ctx.wrap_socket(raw, server_hostname=host) as tls:
            tls.getpeercert()          # completing the handshake IS the test
    except ssl.SSLCertVerificationError as exc:
        _verify_failed(host, port, exc)
    except ssl.SSLError as exc:
        print(f"==> TLS: handshake with {host}:{port} failed — {exc}")
    except OSError as exc:
        print(f"==> NET: cannot reach {host}:{port} — {exc}")
        print("    Check http_proxy/https_proxy and that the host resolves here.")
    else:
        print(f"==> TLS to {host}:{port} verifies — the failure is NOT the CA."
              " Check ~/.netrc credentials and that the index carries the"
              " package.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
