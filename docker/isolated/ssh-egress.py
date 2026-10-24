"""The box's SSH exit (`./run.sh --isolated`): an HTTP CONNECT proxy that opens
SSH connections ONLY — port 22 (AIFORGE_SSH_PORTS) — and nothing else.

The box has no route out. Web traffic leaves through the allowlist proxy;
`ssh`, `scp`, `rsync` and `git@host:` leave through here: the box's ssh
client is configured (docker/entrypoint.sh) with a ProxyCommand that asks this
service to CONNECT host:22.

  AIFORGE_SSH_EGRESS=allowlist  (default) SSH only to hosts on the network
                                allowlist — the ONE list for everything the box
                                reaches: web, git, ssh, scp, rsync
  AIFORGE_SSH_EGRESS=any        SSH to any host
  AIFORGE_SSH_EGRESS=off        no SSH out at all

Only CONNECT is served, only to the allowed ports, and the bytes are relayed
untouched (end-to-end SSH, nothing decrypted). Stdlib only.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import sys

PORT = int(os.environ.get("SSH_EGRESS_LISTEN", "8889"))
POLICY = os.environ.get("EGRESS_POLICY_DIR", "/policy")
_HOST = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


def mode() -> str:
    m = (os.environ.get("AIFORGE_SSH_EGRESS") or "allowlist").strip().lower()
    return m if m in ("any", "allowlist", "off") else "off"


def ports() -> set[int]:
    out = set()
    for p in (os.environ.get("AIFORGE_SSH_PORTS") or "22").split(","):
        p = p.strip()
        if p.isdigit() and 0 < int(p) < 65536:
            out.add(int(p))
    return out or {22}


def _allowlist() -> list[str]:
    try:
        with open(os.path.join(POLICY, "hosts.json")) as f:
            return [str(h).lower() for h in json.load(f).get("hosts") or []]
    except (OSError, ValueError, AttributeError):
        return []


def allowed(host: str, port: int) -> str | None:
    """None when ``host:port`` may be opened, else why not."""
    m = mode()
    if m == "off":
        return "SSH out is off (AIFORGE_SSH_EGRESS=off)"
    if port not in ports():
        return f"only SSH ports {sorted(ports())} are opened here"
    h = host.lower().rstrip(".")
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
    except ValueError:
        ip = None
        if not _HOST.match(h) or "." not in h:
            return "not a host name"
    if ip is not None and (ip.is_loopback or ip.is_link_local or ip.is_unspecified
                           or ip.is_multicast):
        return "not a reachable address"
    if m == "allowlist":
        names = _allowlist()
        if not any(h == n or h.endswith("." + n) for n in names):
            return f"{h} is not on the network allowlist"
    return None


async def _pipe(reader, writer):
    """Copy one direction. At its end only the WRITE side is shut (half
    close): the other direction may still have bytes to deliver."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except (OSError, RuntimeError):
            pass


async def handle(reader, writer):
    peer = writer.get_extra_info("peername")
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        writer.close()
        return
    line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split()
    if len(parts) != 3 or parts[0].upper() != "CONNECT":
        writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
        await writer.drain()
        writer.close()
        return
    target = parts[1]
    host, _, port_s = target.rpartition(":")
    try:
        port = int(port_s)
    except ValueError:
        port = -1
    why = allowed(host, port) if host else "bad target"
    if why:
        print(f"ssh-egress: refused {target} from {peer}: {why}", file=sys.stderr, flush=True)
        writer.write(f"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n{why}\n".encode())
        await writer.drain()
        writer.close()
        return
    try:
        up_r, up_w = await asyncio.wait_for(
            asyncio.open_connection(host.strip("[]"), port), timeout=20)
    except (OSError, asyncio.TimeoutError) as exc:
        writer.write(f"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n{exc}\n".encode())
        await writer.drain()
        writer.close()
        return
    print(f"ssh-egress: open {target} for {peer}", file=sys.stderr, flush=True)
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    await asyncio.gather(_pipe(reader, up_w), _pipe(up_r, writer))
    for w in (up_w, writer):
        try:
            w.close()
        except Exception:  # noqa: BLE001
            pass


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", PORT)
    print(f"ssh-egress: listening on {PORT}, mode={mode()}, ports={sorted(ports())}",
          file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
