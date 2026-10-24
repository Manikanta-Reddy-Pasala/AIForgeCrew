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


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: ssh-connect.py HOST PORT", file=sys.stderr)
        return 2
    host, port = sys.argv[1], sys.argv[2]
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
    out = sys.stdout.buffer
    if rest:
        out.write(rest)
        out.flush()
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


if __name__ == "__main__":
    sys.exit(main())
