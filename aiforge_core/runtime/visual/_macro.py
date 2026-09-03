"""``ui_check`` — one call from "there is a dev server" to "here is what the
screen looks like and what is wrong with it".

The pieces to do this already existed (``serve`` starts a dev server,
``browse`` drives a headless Chromium, a VLM can read an image) but nothing
joined them, so the agent had to chain six calls, guess when the server was
ready, and then receive a quarter-megabyte of base64 it could not see. This
macro owns the join: reuse-or-start the server, poll until it answers,
size the viewport, navigate, let it settle, drain the page's own errors,
capture, audit.
"""
from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urljoin, urlsplit

from ._audit import audit_image
from ._captures import save_capture

_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 800
_DEFAULT_READY_S = 30.0
_DEFAULT_SETTLE_MS = 1200
_MAX_CONSOLE_REPORTED = 8
_MAX_CONSOLE_TEXT = 300


def _f(args: dict, key: str, default: float) -> float:
    try:
        v = args.get(key)
        return default if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return default


def _i(args: dict, key: str, default: int) -> int:
    return int(_f(args, key, float(default)))


def _probe_allowed(url: str) -> bool:
    """May the readiness probe open this URL?

    Defers to the browser's own allowlist so there is ONE answer to "may I open
    this": that covers loopback and the LAN, a host vouched for the duration of
    this call (the dev server `serve` just started), and the operator's own
    allowlist — while still refusing the open web. Duplicating the rule here is
    how the two drift apart and one of them becomes the hole.
    """
    try:
        from aiforge_core.runtime.tools import browser as _browser
        return bool(_browser._allowlist_ok(url))
    except Exception:  # noqa: BLE001 — fail closed for a probe
        return False


def _wait_ready(url: str, timeout_s: float) -> tuple[bool, str]:
    """Poll ``url`` until the server answers ANYTHING.

    An HTTP error IS readiness — a 404 or a 500 proves something is listening
    and is a page worth screenshotting (often the very bug being chased).
    Only a connection failure means "not up yet".

    EGRESS: this probe used to GET whatever URL it was handed, in a retry loop,
    before any allowlist or switch was consulted — so `ui_check` with an
    external URL was a live outbound path with a model-composed query string,
    working fine under the documented lockdown. A non-loopback target now goes
    through the same policy as every other page read.
    """
    if not _probe_allowed(url):
        return False, (
            "refused: this URL is not a local/vouched dev server and web "
            "access is off (see AIFORGE_ALLOW_WEB_FETCH / "
            "AIFORGE_BROWSER_ALLOWLIST). ui_check is for a server on this "
            "machine.")
    deadline = time.monotonic() + max(0.0, timeout_s)
    last = ""
    while True:
        try:
            urllib.request.urlopen(url, timeout=5)
            return True, ""
        except urllib.error.HTTPError:
            return True, ""
        except (ValueError, UnicodeError) as exc:
            # A malformed URL ("localhost:5173" with no scheme) will never
            # start working — retrying it burns the whole 30s timeout before
            # reporting a problem the caller could have been told at once.
            return False, f"{exc} — a URL needs a scheme (http://…)"
        except Exception as exc:  # noqa: BLE001 — URLError/socket/timeout
            last = str(exc)[:200]
        if time.monotonic() >= deadline:
            return False, last
        time.sleep(0.5)


def _pick_alive(url: str, cmd: str,
                alive: list[dict]) -> tuple[dict | None, dict | None]:
    """``(service, error)`` for an already-running server, both None when
    there is nothing to reuse.

    Ambiguity is refused rather than guessed: with an API and a web UI both
    running, picking whichever came first meant ``ui_check({"path": "/login"})``
    could screenshot the backend's 404 page and send the agent off to fix a
    login route that was never broken.
    """
    match = [s for s in alive if not cmd or s.get("cmd") == cmd]
    if not match:
        return None, None
    if not url and not cmd and len(match) > 1:
        return None, {
            "ok": False, "error": "ambiguous_service",
            "services": [{"pid": s.get("pid"), "url": s.get("url"),
                          "cmd": s.get("cmd")} for s in match],
            "hint": ("more than one server is running — pass url= or cmd= "
                     "to say which one to look at")}
    chosen = match[0]
    return {"pid": chosen.get("pid"), "url": chosen.get("url"),
            "cmd": chosen.get("cmd"), "started": False}, None


def _start_service(args: dict, url: str, cmd: str, path: str,
                   cwd: str | None) -> tuple[str, dict | None, dict | None]:
    """Start ``cmd`` and work out the URL to open."""
    from aiforge_core.runtime.tools import serve as _serve

    started = _serve.serve(
        {"cmd": cmd, "port": args.get("port"),
         "wait_s": _f(args, "serve_wait_s", 12.0)}, cwd)
    if not started.get("ok"):
        return "", None, started
    service = {"pid": started.get("pid"), "url": started.get("url"),
               "cmd": cmd, "started": True}
    # An explicit url wins over the one sniffed from the server's log: the
    # caller knows which route they mean, the log only knows the bind address.
    base = url or started.get("url")
    if not base:
        return "", service, {
            "ok": False, "error": "no_url_detected",
            "hint": ("the server started but printed no URL — pass "
                     "port= or url= so ui_check knows where to look"),
            "service": service}
    return _join(base, path), service, None


def _resolve_target(args: dict, cwd: str | None) -> tuple[str, dict | None, dict | None]:
    """``(url, service, error)``.

    ``service`` names the server being looked at — started OR reused — and is
    echoed in the result on every path, so the agent can see which one it got.
    """
    from aiforge_core.runtime.tools import serve as _serve

    url = (args.get("url") or "").strip()
    cmd = (args.get("cmd") or "").strip()
    path = (args.get("path") or "").strip()

    # A url AND a cmd is the common agent shape ("start it, it will be on
    # 5173"). Honour the url when something already answers there, and fall
    # through to starting the cmd when nothing does — returning "connection
    # refused" while holding the command that fixes it is the kind of un-smooth
    # this macro exists to remove.
    if url and (not cmd or _wait_ready(url, _f(args, "reuse_probe_s", 1.0))[0]):
        return _join(url, path), None, None

    listed = _serve.list_services().get("services") or []
    alive = [s for s in listed if s.get("alive") and s.get("url")]
    reused, ambiguous = _pick_alive(url, cmd, alive)
    if ambiguous is not None:
        return "", None, ambiguous
    if reused is not None:
        return _join(url or reused["url"], path), reused, None
    if not cmd:
        return "", None, {
            "ok": False, "error": "missing_url_or_cmd",
            "hint": ("pass url= for a running app, or cmd= (e.g. "
                     "'npm run dev') to have ui_check start it")}
    return _start_service(args, url, cmd, path, cwd)


def _join(base: str, path: str) -> str:
    if not path:
        return base
    if not base.endswith("/"):
        base += "/"
    return urljoin(base, path.lstrip("/"))


_LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _vouched_hosts(url: str, started: dict | None) -> tuple[str, ...]:
    """Hosts this call may browse regardless of the operator allowlist.

    Only two qualify: a loopback address, and the host of a server ``serve``
    started or is tracking. Both are the operator's own dev server, which no
    static allowlist could have named. An arbitrary host the model chose is
    NOT vouched for — it stays subject to the existing allowlist and SSRF
    guard, so this cannot become a way to reach a LAN or metadata endpoint.
    """
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return ()
    if host in _LOOPBACK:
        return (host,)
    if started and (urlsplit(started.get("url") or "").hostname or "").lower() == host:
        return (host,)
    try:
        from aiforge_core.runtime.tools import serve as _serve
        for svc in (_serve.list_services().get("services") or []):
            if (urlsplit(svc.get("url") or "").hostname or "").lower() == host:
                return (host,)
    except Exception:  # noqa: BLE001
        pass
    return ()


def _navigate(browse, url: str, args: dict, run_id: str | None) -> dict | None:
    """Size, navigate, settle. Returns an error dict or None."""
    browse("viewport", width=_i(args, "width", _DEFAULT_WIDTH),
           height=_i(args, "height", _DEFAULT_HEIGHT), _run_id=run_id)
    got = browse("goto", url=url, _run_id=run_id)
    if not got.get("ok"):
        return got
    browse("wait_for", state="networkidle",
           ms=_i(args, "networkidle_ms", 8000), _run_id=run_id)
    settle = _i(args, "settle_ms", _DEFAULT_SETTLE_MS)
    if settle > 0:
        browse("wait_for", ms=settle, _run_id=run_id)
    return None


def _trim_console(entries: list[dict]) -> list[dict]:
    """Cap what reaches the observation. The audit is the payload here and it
    sits in the SAME 6k budget: fifteen 500-char stack traces would push it out
    entirely, which is the failure this whole tool exists to prevent."""
    out = []
    for e in entries[:_MAX_CONSOLE_REPORTED]:
        row = dict(e)
        text = str(row.get("text") or "")
        if len(text) > _MAX_CONSOLE_TEXT:
            row["text"] = text[:_MAX_CONSOLE_TEXT] + "…"
        out.append(row)
    return out


def ui_check(args: dict, cwd: str | None = None,
             run_id: str | None = None) -> dict[str, Any]:
    """Serve (or reuse) → navigate → capture → audit. Never raises.

    ``run_id`` binds this to the SAME browser context the ``browse`` tool uses
    in this chat. Without it ui_check drives the ``default`` context while
    ``browse`` drives ``chat-<id>``: a follow-up click would land on a fresh
    about:blank page, and ``browse console`` would report zero errors from a
    buffer nothing ever wrote to.
    """
    from aiforge_core.runtime.tools import browser as _browser

    url, service, err = _resolve_target(args, cwd)
    if err is not None:
        return err

    # Vouch FIRST, then probe. The readiness probe is an outbound GET like any
    # other, so it now asks the browser allowlist whether it may open the URL —
    # and the vouch (this call's own dev server) has to exist by then, or a
    # non-loopback dev host is refused by the probe and never reaches the
    # screenshot. Everything below is inside the try, so the vouch is still
    # released on every path.
    allow_token = _browser.allow_hosts(_vouched_hosts(url, service))
    try:
        ready, why = _wait_ready(url,
                                 _f(args, "ready_timeout_s", _DEFAULT_READY_S))
        if not ready:
            return {"ok": False, "error": "server_not_reachable", "url": url,
                    "detail": why, "service": service,
                    "hint": ("nothing answered on that URL — check the service "
                             "log or pass a longer ready_timeout_s")}
        nav_err = _navigate(_browser.browse, url, args, run_id)
        if nav_err is not None:
            return {"ok": False, "error": "navigation_failed", "url": url,
                    "detail": nav_err, "service": service}
        png, shot_err = _browser.screenshot_bytes(
            run_id=run_id, full_page=bool(args.get("full_page")))
    finally:
        _browser.reset_allow(allow_token)

    console = _browser.drain_console(run_id, errors_only=False)
    all_errors = [e for e in console if e.get("level") == "error"]
    errors = all_errors

    if png is None:
        return {"ok": False, "error": "screenshot_failed", "url": url,
                "detail": shot_err, "console_errors": _trim_console(all_errors),
                "service": service}

    capture_id, path = save_capture(png, args.get("label") or "ui")
    audit = (audit_image(path, role=str(args.get("role") or "chat"))
             if path else {"ok": False, "error": "capture_not_stored",
                           "hint": ("the screenshot could not be written to the "
                                    "captures directory — check permissions on "
                                    "$AIFORGE_CONFIG_DIR/captures")})
    errors = _trim_console(errors)

    # KEY ORDER MATTERS: the observation is capped (6k chars) and serialized in
    # insertion order, so the audit — the reason this tool exists — goes first.
    # Put it last and a page throwing a dozen stack traces pushes it out
    # entirely, leaving the model with truncated JSON and no idea it lost the
    # answer.
    out: dict[str, Any] = {"ok": True}
    if audit.get("ok"):
        out["audit"] = audit["text"]
        out["vision_role"] = audit.get("vision_role")
    else:
        # The capture and the console errors are still real — a missing or
        # failing VLM downgrades the answer, it does not fail the check. The
        # hint always names the ACTUAL error, never a guessed diagnosis.
        out["audit"] = ""
        out["audit_error"] = audit.get("error")
        out["audit_hint"] = (audit.get("hint") or audit.get("detail")
                             or str(audit.get("error") or "audit failed"))
        out["audit_note"] = ("NOTHING READ THIS SCREEN — do not report it as "
                             "correct: " + str(out["audit_hint"]))
    out.update({
        "url": url, "capture_id": capture_id or "", "screenshot": path or "",
        "console_errors": errors,
        "console_error_count": len(all_errors),
        "viewport": {"width": _i(args, "width", _DEFAULT_WIDTH),
                     "height": _i(args, "height", _DEFAULT_HEIGHT)},
    })
    if len(all_errors) > len(errors):
        out["console_errors_omitted"] = len(all_errors) - len(errors)
    if service is not None:
        out["service"] = service
    return out


__all__ = ["ui_check"]
