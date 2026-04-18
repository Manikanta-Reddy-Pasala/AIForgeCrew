"""Network fetch — gated + auditable outbound HTTP.

DESIGN §8 baseline is "no network tool for any local agent." This module
creates a narrow, audited exception:
  - domain allowlist from `security/network-allowlist.yml`
  - max body size cap (default 500 KB)
  - HEAD/GET only (no POST/PUT/DELETE)
  - URL scheme must be http(s)
  - private-network + localhost rejected unless explicitly listed

Handler is the only place in our code that imports urllib for general-purpose
fetching. `tools/audit_tool_network.py` allowlists this path by name.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

MAX_BODY_BYTES = 500_000
TIMEOUT_S = 15.0
USER_AGENT = "AIForgeCrew/0.1 (+fetch)"


def _load_allowlist(repo_root: Path) -> dict:
    p = repo_root / "security" / "network-allowlist.yml"
    if not p.is_file():
        # Empty default = deny all.
        return {"allow_domains": [], "allow_localhost": False}
    return yaml.safe_load(p.read_text()) or {}


def _host_allowed(host: str, allow_domains: list[str], allow_localhost: bool) -> bool:
    if not host:
        return False
    # Resolve + block private ranges unless explicitly allowed.
    try:
        ip = socket.gethostbyname(host)
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback or addr.is_private:
            return allow_localhost and host in allow_domains
    except (socket.gaierror, ValueError):
        pass

    for pat in allow_domains:
        if pat.startswith("*."):
            if host.endswith(pat[1:]):
                return True
        elif host == pat:
            return True
    return False


class FetchDenied(PermissionError):
    pass


def fetch_url(repo_root: Path, args: dict) -> dict:
    """Fetch a URL with HEAD or GET. Returns status + headers + truncated body."""
    url = args["url"]
    method = (args.get("method") or "GET").upper()
    if method not in ("GET", "HEAD"):
        raise FetchDenied(f"method {method} not allowed (GET|HEAD only)")

    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchDenied(f"scheme {parsed.scheme!r} not allowed")

    rules = _load_allowlist(repo_root)
    allow_domains = list(rules.get("allow_domains") or [])
    allow_localhost = bool(rules.get("allow_localhost", False))

    if not _host_allowed(parsed.hostname or "", allow_domains, allow_localhost):
        raise FetchDenied(f"host {parsed.hostname!r} not in network-allowlist")

    req = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            status = r.status
            headers = dict(r.headers.items())
            body = b"" if method == "HEAD" else r.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as e:
        return {"url": url, "status": e.code, "error": str(e)}
    except urllib.error.URLError as e:
        return {"url": url, "status": 0, "error": str(e)}

    truncated = len(body) > MAX_BODY_BYTES
    return {
        "url": url,
        "method": method,
        "status": status,
        "headers": {k: v for k, v in headers.items() if k.lower() in {
            "content-type", "content-length", "etag", "last-modified", "location",
        }},
        "body": body[:MAX_BODY_BYTES].decode("utf-8", errors="replace"),
        "truncated": truncated,
    }
