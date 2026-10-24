"""The network allowlist's owner, OUTSIDE the box (`./run.sh --isolated`).

In isolated mode every outbound request from the box goes through the egress
proxy, and the proxy lets through only the hosts on its allow file. Whoever can
write that file decides what the agent may reach — so it must never be the box:
the agent has full rights inside it and ~/.aiforge is mounted read-write.

This service keeps the list in its own volume (the proxy and the box get it
read-only) and sits on a network of its own. Settings reaches it through the
UI front door (`/egress-admin/`), which refuses the box's network and the
proxy (docker/ui-proxy.conf.template), so the agent cannot call it.

  GET  /api/hosts      {"hosts": [...]}
  PUT  /api/hosts      {"hosts": [...]} → replace (the proxy re-reads it)
  GET  /api/changes    who changed what, newest first

Optional admin token: when AIFORGE_EGRESS_ADMIN_TOKEN is set, every call must
carry it (``Authorization: Bearer …``). Unset (the default for now): open to
whoever can reach the UI.

A host allows itself and its subdomains (`pypi.org` → `files.pypi.org` too),
nothing else: each entry becomes an ANCHORED pattern, so `pypi.org` never
matches `evilpypi.org` or `pypi.org.attacker.com`. The proxy notices the new
file by itself (docker/egress-proxy.sh).

Stdlib only.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import threading
import os
import re
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
#: A label of only digits / hex digits with 0x: inet_aton reads `127.1`,
#: `0177.0.0.1` or `0x7f.1` as an address — never a host NAME.
_NUMERIC = re.compile(r"^(0x[0-9a-f]*|[0-9]+)$")
_LOCK = threading.Lock()


def token() -> str:
    return os.environ.get("AIFORGE_EGRESS_ADMIN_TOKEN", "")


def ui_hosts() -> set[str]:
    """The names the UI is opened by. A request naming any other Host (or,
    for a change, any other Origin) is refused: a website that rebinds its own
    DNS name to this machine is same-origin with the UI, but not by name."""
    raw = os.environ.get("AIFORGE_UI_HOSTS", "localhost,127.0.0.1")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _hostname(value: str) -> str:
    v = (value or "").strip().lower()
    if "://" in v:
        v = v.split("://", 1)[1]
    v = v.split("/", 1)[0]
    if v.startswith("["):
        return v[1:v.find("]")] if "]" in v else v
    return v.rsplit(":", 1)[0] if v.count(":") == 1 else v


def _never_subnets() -> list:
    out = []
    for var in ("AIFORGE_BOX_SUBNET", "AIFORGE_OUT_SUBNET"):
        try:
            out.append(ipaddress.ip_network(os.environ.get(var, ""), strict=False))
        except ValueError:
            pass
    return out


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
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if ip is not None:
        # This machine, link-local (cloud metadata 169.254.169.254), and the
        # docker networks of this very setup are never destinations.
        if (ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast
                or any(ip in n for n in _never_subnets() if n.version == ip.version)):
            raise ValueError(f"not allowed as a destination: {raw!r}")
        return str(ip)
    if h == "localhost" or h.endswith((".localhost", ".docker.internal")) \
            or h == "docker.internal":
        raise ValueError(f"not allowed as a destination: {raw!r} (this machine)")
    labels = h.split(".")
    if len(h) > 253 or len(labels) < 2 or not all(_LABEL.match(x) for x in labels):
        raise ValueError(f"not a host name: {raw!r}")
    if all(_NUMERIC.match(x) for x in labels):
        raise ValueError(f"looks like an address, not a host name: {raw!r}")
    return h


def pattern(host: str) -> str:
    """The proxy filter line for ``host``: the host or a subdomain of it,
    anchored at both ends."""
    try:
        ipaddress.ip_address(host)
        return "^" + re.escape(host) + "$"
    except ValueError:
        return r"^(.*\.)?" + re.escape(host) + "$"


def load() -> list[str] | None:
    """The saved list; [] when there is none yet, None when the file is
    unreadable (then nothing is allowed until it is saved again — fail
    closed, but the old file is left for a human to look at)."""
    try:
        with open(HOSTS) as f:
            data = json.load(f)
        return [str(h) for h in data.get("hosts") or []]
    except FileNotFoundError:
        return []
    except (OSError, ValueError, AttributeError):
        return None


def _write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def save(hosts: list[str], who: str = "") -> list[str]:
    clean: list[str] = []
    for raw in hosts:
        h = normalise(raw)
        if h not in clean:
            clean.append(h)
    if len(clean) > MAX_HOSTS:
        raise ValueError(f"too many hosts (max {MAX_HOSTS})")
    with _LOCK:
        before = load() or []
        _write(HOSTS, json.dumps({"hosts": clean, "updated": time.time()}, indent=1))
        _write(ALLOW, "".join(pattern(h) + "\n" for h in clean))
        added = [h for h in clean if h not in before]
        removed = [h for h in before if h not in clean]
        if added or removed:
            with open(CHANGES, "a") as f:
                f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    "by": who, "added": added,
                                    "removed": removed}) + "\n")
    return clean


def seed() -> None:
    """First start: the hosts named in AIFORGE_EGRESS_ALLOW_HOSTS. Later
    starts keep what was set in Settings."""
    os.makedirs(POLICY, exist_ok=True)
    if os.path.exists(HOSTS):
        hosts = load()
        if hosts is None:
            print("egress-controller: hosts.json is unreadable — allowing NOTHING "
                  "until the list is saved again", file=sys.stderr)
            _write(ALLOW, "")
            return
        good = []
        for h in hosts:
            try:
                good.append(normalise(h))
            except ValueError as exc:      # saved under older, looser rules
                print(f"egress-controller: dropping {h!r}: {exc}", file=sys.stderr)
        save(good, who="restart")
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
    """The last ``n`` changes, newest first."""
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

    def _headers(self, code: int, ctype: str, length: int) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        # No CORS headers: a page on another origin cannot call this.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()

    def _send(self, code: int, obj) -> None:
        body = json.dumps(obj).encode()
        self._headers(code, "application/json", len(body))
        self.wfile.write(body)

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/"

    def _authorised(self, change: bool = False) -> bool:
        """The request names the UI by one of its own names (Host, and Origin
        for a change), and carries the admin token when one is configured."""
        names = ui_hosts()
        if _hostname(self.headers.get("Host", "")) not in names:
            self._send(403, {"error": "open the UI by its configured name "
                                      "(AIFORGE_UI_HOSTS)"})
            return False
        origin = self.headers.get("Origin")
        if change and origin is not None and _hostname(origin) not in names:
            self._send(403, {"error": "cross-origin change refused"})
            return False
        want = token()
        if not want:
            return True
        got = (self.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        if got and hmac.compare_digest(want.encode(), got.encode()):
            return True
        self._send(401, {"error": "admin token required"})
        return False

    def do_GET(self):  # noqa: N802
        p = self._path()
        if p == "/healthz":
            return self._send(200, {"ok": True})
        if not self._authorised():
            return None
        if p == "/api/hosts":
            hosts = load()
            if hosts is None:
                return self._send(500, {"error": "the saved list is unreadable; save a new one"})
            return self._send(200, {"hosts": hosts, "enforced": True})
        if p == "/api/changes":
            return self._send(200, {"changes": changes()})
        return self._send(404, {"error": "not found"})

    def do_PUT(self):  # noqa: N802
        if self._path() != "/api/hosts":
            return self._send(404, {"error": "not found"})
        if not self._authorised(change=True):
            return None
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > 64 * 1024:
            return self._send(413, {"error": "bad or too large body"})
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        hosts = body.get("hosts") if isinstance(body, dict) else None
        if not isinstance(hosts, list):
            return self._send(400, {"error": "'hosts' must be a list"})
        try:
            saved = save([str(h) for h in hosts], who="settings")
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(200, {"ok": True, "hosts": saved, "enforced": True})


def main() -> None:
    seed()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
