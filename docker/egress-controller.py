"""The network allowlist's owner, OUTSIDE the box (`./run.sh --isolated`).

In isolated mode every outbound request from the box goes through the egress
proxy, and the proxy lets through only the hosts on its allow file. Whoever can
write that file decides what the agent may reach — so it must not be the box:
the agent has full rights inside it, and ~/.aiforge is mounted read-write.

This service holds the list in its own volume (shared read-only with the
proxy), and is reached only through the `ui` front door, which refuses any
request from the box's network (docker/ui-proxy.conf.template). The Settings page in
YOUR browser can change the list; the agent cannot reach this service at all.

  GET  /api/hosts            {"hosts": [...], "enforced": true}
  PUT  /api/hosts            {"hosts": [...]}  → replaces the list, reloads the proxy
  GET  /api/changes          the last changes (who, when, what)

A host allows itself and its subdomains (`pypi.org` → `files.pypi.org` too),
nothing else: each entry becomes an ANCHORED pattern, so `pypi.org` never
matches `evilpypi.org` or `pypi.org.attacker.com`.

Stdlib only; runs as root in its own container so it can signal the proxy
(they share a process namespace: compose `pid: service:egress`).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import signal
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

POLICY = os.environ.get("EGRESS_POLICY_DIR", "/policy")
HOSTS = os.path.join(POLICY, "hosts.json")
ALLOW = os.path.join(POLICY, "allow.txt")
CHANGES = os.path.join(POLICY, "changes.log")
PORT = int(os.environ.get("EGRESS_CONTROLLER_PORT", "8780"))
MAX_HOSTS = 200
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")


def normalise(raw: str) -> str:
    """A host from what was typed (a URL, ``*.host``, ``host:port``), lower
    case; raises ValueError for anything that is not a host name or an IP."""
    h = str(raw or "").strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    h = h.rsplit("@", 1)[-1]
    if h.startswith("[") and "]" in h:            # [v6]:port
        h = h[1:h.index("]")]
    elif h.count(":") == 1:
        h = h.split(":", 1)[0]
    if h.startswith("*."):
        h = h[2:]
    h = h.rstrip(".")
    if not h:
        raise ValueError(f"not a host: {raw!r}")
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    labels = h.split(".")
    if len(h) > 253 or len(labels) < 2 or not all(_LABEL.match(x) for x in labels):
        raise ValueError(f"not a host name: {raw!r}")
    return h


def pattern(host: str) -> str:
    """The proxy filter line for ``host``: the host or a subdomain of it,
    anchored at both ends."""
    try:
        ipaddress.ip_address(host)
        return "^" + re.escape(host) + "$"
    except ValueError:
        return r"^(.*\.)?" + re.escape(host) + "$"


def load() -> list[str]:
    try:
        with open(HOSTS) as f:
            return list(json.load(f).get("hosts") or [])
    except (OSError, ValueError):
        return []


def _write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def reload_proxy() -> bool:
    """Ask the proxy to re-read its filter (tinyproxy: SIGUSR1)."""
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/comm") as f:
                if f.read().strip() == "tinyproxy":
                    os.kill(int(pid), signal.SIGUSR1)
                    return True
        except OSError:
            continue
    return False


def save(hosts: list[str], who: str = "") -> list[str]:
    clean: list[str] = []
    for raw in hosts:
        h = normalise(raw)
        if h not in clean:
            clean.append(h)
    if len(clean) > MAX_HOSTS:
        raise ValueError(f"too many hosts (max {MAX_HOSTS})")
    before = load()
    _write(ALLOW, "".join(pattern(h) + "\n" for h in clean))
    _write(HOSTS, json.dumps({"hosts": clean, "updated": time.time()}, indent=1))
    added = [h for h in clean if h not in before]
    removed = [h for h in before if h not in clean]
    if added or removed:
        with open(CHANGES, "a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                "by": who, "added": added,
                                "removed": removed}) + "\n")
    reload_proxy()
    return clean


def seed() -> None:
    """First start: the hosts named in AIFORGE_EGRESS_ALLOW_HOSTS. Later
    starts keep what was set in Settings."""
    os.makedirs(POLICY, exist_ok=True)
    if os.path.exists(HOSTS):
        save(load(), who="restart")
        return
    raw = [h for h in os.environ.get("AIFORGE_EGRESS_ALLOW_HOSTS", "").split(",") if h.strip()]
    good = []
    for h in raw:
        try:
            good.append(normalise(h))
        except ValueError:
            print(f"egress-controller: skipping {h!r}", file=sys.stderr)
    save(good, who="AIFORGE_EGRESS_ALLOW_HOSTS")


def changes(n: int = 50) -> list[dict]:
    try:
        with open(CHANGES) as f:
            lines = f.readlines()[-n:]
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "aiforge-egress-controller"

    def log_message(self, fmt, *args):  # noqa: D401 — quieter access log
        sys.stderr.write("egress-controller: " + (fmt % args) + "\n")

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/")

    def do_GET(self):  # noqa: N802
        p = self._path()
        if p == "/api/hosts":
            return self._send(200, {"hosts": load(), "enforced": True})
        if p == "/api/changes":
            return self._send(200, {"changes": changes()})
        return self._send(404, {"error": "not found"})

    def do_PUT(self):  # noqa: N802
        if self._path() != "/api/hosts":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 64 * 1024:
                return self._send(413, {"error": "too large"})
            body = json.loads(self.rfile.read(n) or b"{}")
            hosts = body.get("hosts")
            if not isinstance(hosts, list):
                return self._send(400, {"error": "'hosts' must be a list"})
            who = self.headers.get("X-Real-IP") or self.client_address[0]
            saved = save([str(h) for h in hosts], who=who)
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(200, {"ok": True, "hosts": saved, "enforced": True})


def main() -> None:
    seed()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
