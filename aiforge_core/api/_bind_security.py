"""Who may reach the API: the bind host, loopback trust, the sync paths that
stay open, and the request token check's helpers."""
from __future__ import annotations

import ipaddress
import logging
import os

from fastapi import Request

from aiforge_core.api.routes import admin as _r_admin

# ─────────────────────── API auth + bind-host guard ─────────────────────
# This control plane RUNS SHELL and EDITS FILES over HTTP, so exposing it
# unauthenticated is a remote-code-execution surface. Design (pragmatic, must
# not break local dev / the UI / the tests):
#   * AIFORGE_API_TOKEN set  → every /api/* route (except health) requires
#     EITHER a matching ``Authorization: Bearer <token>`` (or
#     ``X-AIForge-Token``) OR — only while AIFORGE_TRUST_LOOPBACK is on — a
#     loopback peer address. Loopback is trusted by default because reaching
#     the socket from this machine already implies read/write access to the
#     same files over the filesystem.
#   * THE ADMIN SURFACE (``/admin`` + ``/api/admin/*``) ALWAYS requires the
#     token when one is configured, loopback or not: it is the highest-value
#     screen and must not rest on the weakest signal we have.
#   * token unset → open (preserves local dev + the UI on localhost); a
#     non-loopback bind in that state is refused at boot instead.
#   * NON-loopback bind + no token → REFUSE TO BOOT (see _security_boot_guard).
# The UI static assets, ``/files`` and ``/`` stay open (no token) so the app
# shell can load; the browser then sends the operator-configured token on API
# calls. A single shared token — not user accounts. Keep it simple.


def _api_token() -> str:
    return os.environ.get("AIFORGE_API_TOKEN", "").strip()


def _sync_open() -> bool:
    """Whether the hub sync surface answers without a credential.

    **Open by default.** The admin's whole job is to receive every machine's
    memory and serve back what it distilled, and the deployment this was built
    for puts it on a trusted interface (a LAN or a WireGuard address) where the
    spokes need no secret to keep in step. ``AIFORGE_SYNC_AUTH=1`` closes it
    again, and then the ordinary API token is what a spoke must present.

    This is a *scoped* decision: it opens ``/api/memory/sync/*`` and nothing
    else. The control plane — which runs shells and writes config — still
    requires ``AIFORGE_API_TOKEN`` from every non-loopback caller, so an open
    sync surface never becomes an open shell.
    """
    return not _flag_on("AIFORGE_SYNC_AUTH", "0")


def _is_sync_path(path: str) -> bool:
    """The hub sync surface ``AIFORGE_SYNC_AUTH=0`` opens (and ONLY it).

    Matched on the raw request path, so a dot-segment or encoded-traversal
    variant (``/api/memory/sync/../chat/agent``) is rejected here rather than
    trusted to dead-end at the router: Starlette does not collapse ``..``, but a
    fronting proxy might, and an open sync path must never be a path that could
    dispatch to the control plane. The legitimate sync paths contain none of
    these, so refusing them costs nothing.
    """
    if not path.startswith("/api/memory/sync/"):
        return False
    lowered = path.lower()
    return not ("//" in path or ".." in path
                or "%2e" in lowered or "%2f" in lowered or "%5c" in lowered)


def _flag_on(name: str, default: str = "1") -> bool:
    return (os.environ.get(name) or default).strip().lower() \
        not in ("0", "false", "no", "off")


def _trust_loopback() -> bool:
    """Whether a loopback TCP peer counts as authenticated (AIFORGE_TRUST_LOOPBACK).

    Default ON so a bare local run keeps working with no configuration. It MUST
    be set to ``0`` on any deployment that is fronted by a reverse proxy on the
    same host (Cloudflare → nginx → this app is the documented one): the peer
    address the app sees is then the proxy's ``127.0.0.1`` for every request on
    earth, so implicit loopback trust becomes a full auth bypass. The trust is
    a deliberate configuration statement, never an accident of topology.
    """
    return _flag_on("AIFORGE_TRUST_LOOPBACK")


def _bind_host() -> str:
    """Pre-boot HINT for the host uvicorn binds to (AIFORGE_BIND_HOST, set by
    run.sh / docker-compose). Only a hint: the real listening address is read
    off the running server by ``_observed_bind_hosts`` — an env var says nothing
    about what a ``uvicorn --host 0.0.0.0`` actually did."""
    return (os.environ.get("AIFORGE_BIND_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def _find_uvicorn_server():
    """Walk the coroutine frames of the live asyncio tasks for the running
    uvicorn ``Server``. Startup hooks run inside uvicorn's lifespan task, not
    under ``Server.startup``, so the server is not on our own stack. None under
    TestClient / no running loop / anything we cannot introspect."""
    try:
        import asyncio
        tasks = asyncio.all_tasks()
    except Exception:  # noqa: BLE001
        return None
    for task in tasks:
        coro = task.get_coro()
        while coro is not None:
            frame = getattr(coro, "cr_frame", None)
            if frame is None:
                break
            obj = frame.f_locals.get("self")
            cls = type(obj)
            if cls.__name__ == "Server" and cls.__module__.split(".")[0] == "uvicorn":
                return obj
            coro = getattr(coro, "cr_await", None)
    return None


def _server_socket_hosts(server) -> list[str]:
    """The hosts of the server's REAL listening sockets (``getsockname``)."""
    hosts: list[str] = []
    for asgi_server in (getattr(server, "servers", None) or []):
        for sock in (getattr(asgi_server, "sockets", None) or []):
            try:
                hosts.append(str(sock.getsockname()[0]))
            except Exception:  # noqa: BLE001 — a unix socket has no host tuple
                continue
    return hosts


def _observed_bind_hosts() -> list[str]:
    """The addresses this process is REALLY listening on, or ``[]`` if unknown.

    ``AIFORGE_BIND_HOST`` is exported by run.sh only, so a systemd unit, a
    Dockerfile CMD or a developer typing ``uvicorn --host 0.0.0.0`` used to
    satisfy the boot guard with the loopback default while publishing a
    shell-running control plane to the LAN. So ask the server, not the env.

    Real listening sockets win when they exist; ``config.host`` is the answer
    during startup, before the sockets are created. Returns ``[]`` under
    TestClient / gunicorn / anything else we cannot introspect, which the caller
    must treat as "unobserved", not as "loopback".
    """
    server = _find_uvicorn_server()
    if server is None:
        return []
    hosts = _server_socket_hosts(server)
    if hosts:
        return hosts
    config = getattr(server, "config", None)
    if getattr(config, "uds", None) or getattr(config, "fd", None) is not None:
        return []                      # not an inet bind we can reason about
    host = getattr(config, "host", None)
    return [str(host)] if host else []


def _is_loopback_host(host: str) -> bool:
    h = (host or "").strip().lower()
    if h in ("", "localhost", "127.0.0.1", "::1"):
        return True
    if h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _compute_exposed_hosts(hosts, token, boot_log):
    """The non-loopback hosts this process is actually exposed on (observed binds, or the AIFORGE_BIND_HOST fallback with a warning when no token)."""
    observed = _observed_bind_hosts() if hosts is None else list(hosts)
    if observed:
        exposed = [h for h in observed if not _is_loopback_host(h)]
    else:
        # Nothing to observe (TestClient, gunicorn, an embedder): fall back to
        # the pre-boot hint and SAY SO, because the fallback is the thing that
        # used to be trusted silently.
        env_host = _bind_host()
        exposed = [] if _is_loopback_host(env_host) else [env_host]
        if not token:
            boot_log.warning(
                "could not observe the real listening address; falling back to "
                "AIFORGE_BIND_HOST=%s for the security guard — if this process "
                "actually binds a non-loopback address, set AIFORGE_API_TOKEN.",
                env_host)
    return exposed


def _log_sync_openness(boot_log):
    """Log whether the open (credential-less) memory-sync endpoints are reachable only from loopback (info) or bound to a non-loopback host (warning)."""
    if _sync_open():
        # Not a refusal — it is the documented default (see ``_sync_open``) —
        # but the severity depends entirely on what this box is bound to, so the
        # line says which case it is rather than stating the setting and leaving
        # the operator to work it out.
        _bound = _bind_host()
        if _is_loopback_host(_bound):
            boot_log.info("memory sync is open (no credential) on "
                          "/api/memory/sync/* — reachable from this machine "
                          "only, since the bind host is %s.", _bound)
        else:
            boot_log.warning(
                "memory sync is OPEN (no credential) on /api/memory/sync/* AND "
                "bound to %s. Anything that can reach this port can WRITE "
                "memory that the merge folds into every machine's working "
                "knowledge. Keep this on a trusted interface (LAN/WireGuard), "
                "or set AIFORGE_SYNC_AUTH=1 here and AIFORGE_API_TOKEN on every "
                "machine.", _bound)



def _security_boot_guard(hosts: list[str] | None = None) -> None:
    """Refuse to boot when a shell-running control plane is listening on a
    non-loopback address without a token. Raises ``RuntimeError`` — called from
    a startup hook (where the REAL bind is observable) AND directly
    unit-testable by passing ``hosts``."""
    token = _api_token()
    boot_log = logging.getLogger("aiforge.boot")

    _log_sync_openness(boot_log)
    exposed = _compute_exposed_hosts(hosts, token, boot_log)
    if not exposed:
        return
    where = ", ".join(exposed)
    # Escape hatch: the operator fronts the api with their OWN access layer
    # (Cloudflare Access / a WireGuard-only reverse proxy / nginx auth) and
    # accepts responsibility for exposure. Explicit opt-out so a bind to a
    # tunnel/LAN interface works without the app requiring a token.
    fronted = os.environ.get("AIFORGE_ALLOW_UNAUTH_NONLOOPBACK", "").strip().lower() \
        in ("1", "true", "yes", "on")
    if not token and not fronted:
        raise RuntimeError(
            f"AIForge refuses to boot: listening on a non-loopback host ({where}) "
            "exposes a shell-running control plane. Set AIFORGE_API_TOKEN to a "
            "shared secret (and configure the UI with it), bind 127.0.0.1, OR "
            "set AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1 if you front it yourself "
            "(Cloudflare / WireGuard-only proxy)."
        )
    if not token and fronted:
        boot_log.warning(
            "api listening on %s WITHOUT a token (AIFORGE_ALLOW_UNAUTH_NONLOOPBACK=1) "
            "— ensure your own access layer (Cloudflare/WireGuard/nginx) fronts it, "
            "and set AIFORGE_TRUST_LOOPBACK=0 so the proxy's loopback peer address "
            "does not read as authenticated.", where)
    elif token and _trust_loopback():
        boot_log.warning(
            "api listening on %s with AIFORGE_TRUST_LOOPBACK on — if a reverse "
            "proxy on THIS host forwards to it, every request arrives from "
            "127.0.0.1 and skips the token; set AIFORGE_TRUST_LOOPBACK=0.", where)


def _is_admin_path(path: str) -> bool:
    """The operator admin surface: the page and its data endpoint."""
    return path == "/admin" or path.startswith("/admin/") or path.startswith("/api/admin")


def _auth_exempt(path: str) -> bool:
    """Routes reachable without a token even when one is configured: health,
    the UI shell / static assets and the root redirect. Everything else under
    ``/api/`` is protected — as is the admin surface, which lives outside
    ``/api/`` but is never exempt."""
    if _is_admin_path(path):
        return False
    if path == "/api/health":
        return True
    # The hub sync surface, unless the operator closed it. Exempt rather than
    # "authenticated by a second credential": there is no mesh key any more, so
    # a spoke either needs the control-plane token (AIFORGE_SYNC_AUTH=1) or
    # nothing at all — and "nothing at all" is exactly an exemption.
    if _sync_open() and _is_sync_path(path):
        return True
    return not path.startswith("/api/")


def _request_is_loopback(request: Request) -> bool:
    """True when the request's TCP peer is this machine.

    Delegates to ``routes.admin._require_loopback`` — the admin page already
    owns this predicate, and a security check with two implementations WILL
    drift. That helper decides purely from ``request.client.host`` (the real
    peer address); X-Forwarded-For / X-Real-IP / Host / Forwarded are
    attacker-controlled and are deliberately never consulted. It raises
    ``HTTPException`` for "not local", which is adapted to a bool here.
    """
    from fastapi import HTTPException as _HTTPException
    try:
        _r_admin._require_loopback(request)
    except _HTTPException:
        return False
    return True


def _extract_request_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth[:7].lower() == "bearer ":
        return auth[7:].strip()
    return (request.headers.get("x-aiforge-token", "") or "").strip()
