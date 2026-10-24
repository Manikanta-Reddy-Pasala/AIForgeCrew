#!/usr/bin/env python3
"""ssh ProxyCommand for the isolated box: `ssh-connect.py HOST PORT`.

The box has no route out; its ssh client is set up (docker/entrypoint.sh) to
reach every host through the SSH exit (docker/ssh-egress.py) with an HTTP
CONNECT, then relay stdin/stdout. Stdlib only — the box has no nc/socat.
"""
import os
import selectors
import socket
import sys


def _private(host: str, port: str) -> bool:
    """Every address ``host`` resolves to is this machine or the LAN (the
    guarded default lets those through directly; only the internet is gated)."""
    import ipaddress
    try:
        infos = socket.getaddrinfo(host, int(port), type=socket.SOCK_STREAM)
    except (OSError, ValueError):
        return False
    addrs = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    return bool(addrs) and all(a.is_private or a.is_loopback or a.is_link_local for a in addrs)


def _relay(s) -> int:
    out = sys.stdout.buffer
    sel = selectors.DefaultSelector()
    sel.register(s, selectors.EVENT_READ)
    sel.register(sys.stdin.buffer, selectors.EVENT_READ)
    stdin_fd = sys.stdin.fileno()
    while True:
        for key, _ in sel.select():
            if key.fileobj is s:
                data = s.recv(65536)
                if not data:
                    return 0
                out.write(data)
                out.flush()
            else:
                data = os.read(stdin_fd, 65536)
                if not data:
                    try:
                        s.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    sel.unregister(sys.stdin.buffer)
                    continue
                s.sendall(data)


def _direct(host: str, port: str) -> int:
    try:
        s = socket.create_connection((host, int(port)), timeout=20)
    except OSError as exc:
        print(f"ssh-connect: {host}:{port}: {exc}", file=sys.stderr)
        return 1
    s.settimeout(None)
    return _relay(s)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: ssh-connect.py HOST PORT", file=sys.stderr)
        return 2
    host, port = sys.argv[1], sys.argv[2]
    if os.environ.get("AIFORGE_SSH_DIRECT_PRIVATE") == "1" and _private(host, port):
        return _direct(host, port)
    proxy = os.environ.get("AIFORGE_SSH_PROXY", "ssh-egress:8889")
    phost, _, pport = proxy.rpartition(":")
    try:
        s = socket.create_connection((phost, int(pport)), timeout=20)
    except OSError as exc:
        print(f"ssh-connect: cannot reach the SSH exit {proxy}: {exc}", file=sys.stderr)
        return 1
    target = f"[{host}]:{port}" if ":" in host and not host.startswith("[") else f"{host}:{port}"
    s.sendall(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = s.recv(4096)
        if not chunk:
            break
        head += chunk
    status, _, rest = head.partition(b"\r\n\r\n")
    if b" 200 " not in status.split(b"\r\n", 1)[0] + b" ":
        print("ssh-connect: refused: " + (rest or status).decode("latin-1", "replace").strip(),
              file=sys.stderr)
        return 1
    s.settimeout(None)
    if rest:
        sys.stdout.buffer.write(rest)
        sys.stdout.buffer.flush()
    return _relay(s)


if __name__ == "__main__":
    sys.exit(main())
