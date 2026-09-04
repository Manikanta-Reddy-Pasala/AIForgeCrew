"""Headless Playwright browser tool for the Doer agent (OH parity sub #2).

Single entry point :func:`browse` with a ``command`` dispatcher mirroring the
:mod:`editor` shape. One Playwright ``BrowserContext`` per ADK run, lazily
created and destroyed by :func:`destroy_context`.

Soft-error contract everywhere. Playwright is an optional dependency: when
absent, every call returns ``{ok: False, error: "playwright_missing"}`` so the
agent loop survives on dev boxes without the install.
"""
from __future__ import annotations

import base64
import contextvars
import os
import re
import uuid
from typing import Any

from aiforge_core.runtime.sandbox import resolve_inside_root

from ._trace import emit

# Run the browser context belongs to. The Doer FunctionTool wrapper omits
# ``_run_id``, so fall back to a contextvar the runner sets, then a STABLE
# "default" — a fresh uuid per call created a new BrowserContext each command
# (breaking goto→extract flows) and leaked it (destroy keys on session id).
_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "browser_run_id", default=None)

# Hosts an in-process caller vouches for, for the duration of ITS call — the
# dev server ``ui_check`` just started, which no allowlist could name in
# advance. A ContextVar rather than a mutated env var because the API serves
# concurrent chats in threads: an env write is process-global, so one chat
# would grant (and later revoke) a host out from under another. Same reasoning
# as aiforge_core/runtime/request_context.
_EXTRA_ALLOW: contextvars.ContextVar[tuple[str, ...]] = contextvars.ContextVar(
    "browser_extra_allow", default=())


def set_run_id(run_id: str | None) -> None:
    _RUN_ID.set(run_id)


def _effective_run_id(explicit: str | None) -> str:
    return explicit or _RUN_ID.get() or "default"


def allow_hosts(hosts):
    """Vouch for ``hosts`` (any iterable of names) in the current context.
    Returns the ContextVar token — reset it in a ``finally`` so the grant dies
    with the call."""
    clean = tuple(h.strip().lower() for h in hosts if h and h.strip())
    return _EXTRA_ALLOW.set(_EXTRA_ALLOW.get() + clean)


def reset_allow(token) -> None:
    import contextlib
    with contextlib.suppress(ValueError, LookupError):  # token from elsewhere
        _EXTRA_ALLOW.reset(token)

_SCREENSHOT_CAP_BYTES = 256 * 1024
_TEXT_CAP_BYTES = 32 * 1024

# Page-level diagnostics (console messages, uncaught JS errors, failed
# requests) per run. A UI bug usually announces itself HERE rather than in the
# DOM — a 404 on a JS chunk, a hydration mismatch, a thrown render error — so
# the listeners are attached at context-creation time, BEFORE the first goto.
# Attaching them later would miss every error the page threw while loading,
# which is precisely the class of failure this exists to catch.
_CONSOLE_RING = 200          # entries kept per run; oldest dropped
_CONSOLE_TEXT_CAP = 500      # per-entry text cap
_console: dict[str, list[dict[str, Any]]] = {}

# Browser-initiated cancellations, not page defects.
_ABORTED_RE = re.compile(r"(?i)ERR_ABORTED|ERR_CANCELED|ERR_CANCELLED"
                         r"|net::ERR_BLOCKED_BY_CLIENT")

# Module-level Playwright instances, keyed by ADK run_id.
_contexts: dict[str, Any] = {}
_pw_handle: Any = None
_browser: Any = None


def _playwright_available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except ImportError:
        return False


def _matches_operator_allowlist(host: str, raw: str) -> bool:
    """Exact-or-subdomain match of ``host`` against the operator's list.

    Matched against the HOSTNAME, never ``re.search`` over the whole URL —
    that let ``http://169.254.169.254/#github.com`` match a ``github.com``
    entry (SSRF to cloud IMDS via a fragment). A host the operator EXPLICITLY
    allowlisted is trusted even if it is a LAN/loopback dev server; that is
    the intended use of the browser tool.
    """
    for pattern in raw.split(","):
        pattern = pattern.strip().lower()
        if not pattern:
            continue
        if host == pattern or host.endswith("." + pattern):
            return True
    return False


def _is_local_host(host: str) -> bool:
    """Delegates to net.egress so there is ONE definition of "not egress".
    Kept as a name here because the tests and the allowlist logic both use it;
    two copies of this rule is how they drift and one becomes the hole."""
    from aiforge_core.net import egress as _egress
    return _egress.is_local_host(host)


def _open_browsing_ok(url: str) -> bool:
    """With no operator allowlist, browsing is off unless the operator opts in
    via ``AIFORGE_ALLOW_WEB_FETCH`` — and even then the target is SSRF-guarded
    so a model-chosen URL cannot pivot to cloud metadata or a private LAN host.
    A pure DNS failure falls through to the browser, which will simply fail to
    connect: it cannot be an SSRF target.
    """
    from aiforge_core.net import egress as _egress
    if not _egress.fetch_allowed():
        return False
    from aiforge_core.net.ssl import SSRFBlocked, guard_public_url
    try:
        guard_public_url(url)
    except SSRFBlocked as exc:
        return exc.kind == "dns"
    return True


def _allowlist_ok(url: str) -> bool:
    from urllib.parse import urlsplit

    from aiforge_core.net import egress as _egress

    host = (urlsplit(url).hostname or "").lower()
    # A host the current call vouched for (its own dev server) — checked before
    # the operator allowlist and never persisted anywhere. Loopback work
    # (ui_check, a local dev server) is NOT web egress and stays available even
    # under the lockdown; that is the whole point of vouching.
    if host and host in _EXTRA_ALLOW.get():
        return True
    # A search engine is refused here too: web search was removed, and driving
    # one through a headless browser is the same capability with more steps.
    if _egress.looks_like_search(url):
        return False
    # Loopback is not egress, whatever the switches say and whether or not the
    # operator keeps an allowlist. Checked BEFORE the allowlist branch: an
    # operator who locks the box down and clears the browser allowlist (the
    # natural thing to do) would otherwise lose ui_check against their own dev
    # server — a control doing something nobody asked it to do.
    if _is_local_host(host):
        return True
    # The EGRESS allowlist applies here too. Before the list defaulted to deny
    # this was moot — everything was allowed, so browse and web_fetch agreed.
    # Now they would disagree, and browse is the widest page tool in the system:
    # web_fetch would refuse attacker.example while browse drove a real page
    # there. An operator's browser allowlist narrows further; it cannot widen.
    if not _egress.host_allowed(url):
        return False
    raw = os.environ.get("AIFORGE_BROWSER_ALLOWLIST", "").strip()
    if raw:
        # An explicit operator allowlist IS the permission for those hosts —
        # naming a host is a deliberate act, unlike a model-chosen URL, so it is
        # not second-guessed by AIFORGE_ALLOW_WEB_FETCH. The HARD-off is
        # different: it means this box must not talk out at all, and it wins
        # over every allowlist.
        if _egress.hard_off():
            return False
        return _matches_operator_allowlist(host, raw)
    return _open_browsing_ok(url)


def _record(run_id: str, entry: dict[str, Any]) -> None:
    """Append one diagnostic entry, oldest-out at :data:`_CONSOLE_RING`.

    Never raises: this runs inside a Playwright event callback, where an
    exception would surface on an unrelated later call.
    """
    try:
        buf = _console.setdefault(run_id, [])
        text = str(entry.get("text") or "")[:_CONSOLE_TEXT_CAP]
        entry["text"] = text
        buf.append(entry)
        if len(buf) > _CONSOLE_RING:
            del buf[:len(buf) - _CONSOLE_RING]
    except Exception:  # noqa: BLE001 — diagnostics must never break a page
        pass


def _attach_listeners(page: Any, run_id: str) -> None:
    """Wire console / pageerror / requestfailed into the run's ring buffer."""
    def _on_console(msg: Any) -> None:
        try:
            kind = msg.type
        except Exception:  # noqa: BLE001
            kind = "log"
        _record(run_id, {"kind": "console", "level": str(kind),
                         "text": str(getattr(msg, "text", "") or "")})

    def _on_pageerror(err: Any) -> None:
        _record(run_id, {"kind": "pageerror", "level": "error",
                         "text": str(err)})

    def _on_requestfailed(req: Any) -> None:
        try:
            failure = req.failure
        except Exception:  # noqa: BLE001
            failure = ""
        text = str(failure or "request failed")
        # A request the BROWSER cancelled — navigating away, an HMR socket torn
        # down at page close — is routine, not a defect. Levelling it "error"
        # would spend the agent's two fix rounds chasing a phantom.
        level = "info" if _ABORTED_RE.search(text) else "error"
        _record(run_id, {"kind": "requestfailed", "level": level,
                         "url": str(getattr(req, "url", ""))[:300],
                         "text": text})

    for event, handler in (("console", _on_console),
                           ("pageerror", _on_pageerror),
                           ("requestfailed", _on_requestfailed)):
        try:
            page.on(event, handler)
        except Exception:  # noqa: BLE001 — a driver without the event
            continue


def drain_console(run_id: str | None = None, *, clear: bool = True,
                  errors_only: bool = False) -> list[dict[str, Any]]:
    """Return the diagnostics collected for ``run_id`` (and clear them by
    default, so a second page-load reports only its OWN errors)."""
    rid = _effective_run_id(run_id)
    buf = _console.get(rid) or []
    out = [e for e in buf if not errors_only or e.get("level") == "error"]
    if clear:
        _console[rid] = []
    return out


def _teardown_globals() -> None:
    """Close + null the shared browser/playwright handles (best-effort)."""
    global _pw_handle, _browser
    if _browser is not None:
        try:
            _browser.close()
        except Exception:  # noqa: BLE001
            pass
        _browser = None
    if _pw_handle is not None:
        try:
            _pw_handle.stop()
        except Exception:  # noqa: BLE001
            pass
        _pw_handle = None


def _get_context(run_id: str) -> Any:
    """Lazy-create the per-run BrowserContext. Returns the context or raises."""
    global _pw_handle, _browser
    if run_id in _contexts:
        return _contexts[run_id]
    from playwright.sync_api import sync_playwright
    # On ANY partial-init failure, tear down + null the shared handles so a
    # half-launched browser doesn't leak its driver subprocess and poison
    # every later call (which would reuse the dead _browser forever).
    try:
        if _pw_handle is None:
            _pw_handle = sync_playwright().start()
        if _browser is None:
            _browser = _pw_handle.chromium.launch(headless=True)
        ctx = _browser.new_context()
        page = ctx.new_page()
        _console[run_id] = []
        _attach_listeners(page, run_id)
    except Exception:
        _teardown_globals()
        raise
    _contexts[run_id] = (ctx, page)
    emit("Browse", {"action": "context_created", "run_id": run_id})
    return _contexts[run_id]


def destroy_context(run_id: str) -> None:
    """Close BrowserContext + cleanup global handles when last run is gone."""
    _console.pop(run_id, None)
    pair = _contexts.pop(run_id, None)
    if pair is not None:
        ctx, _page = pair
        try:
            ctx.close()
        except Exception:  # noqa: BLE001 — best-effort cleanup
            pass
        emit("Browse", {"action": "context_destroyed", "run_id": run_id})
    # Tear down the shared browser whenever no contexts remain — even if this
    # run never successfully created one (a partial-init failure would
    # otherwise leave the launched browser/driver running forever).
    if not _contexts:
        _teardown_globals()


def _goto(page: Any, url: str) -> dict[str, Any]:
    if not _allowlist_ok(url):
        return {"ok": False, "error": "url_not_in_allowlist", "url": url}
    response = page.goto(url, timeout=30000)
    status = response.status if response else None
    return {
        "ok": True, "url": page.url, "title": page.title(), "status": status,
    }


def _screenshot(page: Any, path: str | None,
                full_page: bool = False) -> dict[str, Any]:
    png = page.screenshot(full_page=bool(full_page))
    out_path = None
    if path:
        try:
            p = resolve_inside_root(path)
        except PermissionError:
            return {"ok": False, "error": "path_traversal", "path": path}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(png)
        out_path = path
    b64 = base64.b64encode(png[:_SCREENSHOT_CAP_BYTES]).decode("ascii")
    return {
        "ok": True, "path": out_path,
        "png_b64": b64, "bytes": len(png),
        "truncated": len(png) > _SCREENSHOT_CAP_BYTES,
    }


def _viewport(page: Any, width: int, height: int) -> dict[str, Any]:
    """Resize the page. A screenshot at the driver's default 1280x720 says
    nothing about the phone layout the user is complaining about."""
    w, h = int(width or 0) or 1280, int(height or 0) or 800
    page.set_viewport_size({"width": w, "height": h})
    return {"ok": True, "width": w, "height": h}


# Load states Playwright's wait_for_load_state accepts.
_LOAD_STATES = ("load", "domcontentloaded", "networkidle")


def _wait_for(page: Any, selector: str | None, state: str | None,
              ms: int | None) -> dict[str, Any]:
    """Wait for a selector, a load state, or a fixed delay — so a capture is
    taken of the SETTLED page rather than a half-painted one."""
    if selector:
        page.wait_for_selector(selector, timeout=int(ms or 10000))
        return {"ok": True, "waited": "selector", "selector": selector}
    if state:
        if state not in _LOAD_STATES:
            return {"ok": False, "error": "unknown_state", "state": state,
                    "allowed": list(_LOAD_STATES)}
        page.wait_for_load_state(state, timeout=int(ms or 15000))
        return {"ok": True, "waited": "state", "state": state}
    page.wait_for_timeout(int(ms or 500))
    return {"ok": True, "waited": "timeout", "ms": int(ms or 500)}


def _console_cmd(run_id: str, clear: bool, errors_only: bool) -> dict[str, Any]:
    entries = drain_console(run_id, clear=clear, errors_only=errors_only)
    out: dict[str, Any] = {"ok": True, "count": len(entries), "entries": entries}
    if not entries:
        # Reading DRAINS, and ui_check drains on every capture so each check
        # reports only its own page load. Empty therefore means "nothing since
        # the last read", NOT "this page is clean" — a distinction an agent
        # told to fix console errors will otherwise get exactly backwards.
        out["note"] = ("empty: nothing logged since the last read. ui_check "
                       "drains this buffer — the errors from the last check "
                       "are in its result, not here.")
    return out


def _click(page: Any, selector: str) -> dict[str, Any]:
    page.click(selector, timeout=10000)
    return {"ok": True, "selector": selector}


def _fill(page: Any, selector: str, text: str) -> dict[str, Any]:
    page.fill(selector, text, timeout=10000)
    return {"ok": True, "selector": selector}


def _extract_text(page: Any, selector: str | None) -> dict[str, Any]:
    if selector:
        text = page.inner_text(selector, timeout=10000)
    else:
        text = page.inner_text("body", timeout=10000)
    if len(text.encode("utf-8")) > _TEXT_CAP_BYTES:
        text = text.encode("utf-8")[:_TEXT_CAP_BYTES].decode("utf-8", "replace")
        truncated = True
    else:
        truncated = False
    return {"ok": True, "text": text, "truncated": truncated}


def _mouse_click(page: Any, x: int, y: int, button: str) -> dict[str, Any]:
    page.mouse.click(x, y, button=button or "left")
    return {"ok": True, "x": x, "y": y, "button": button or "left"}


def _key_press(page: Any, key: str) -> dict[str, Any]:
    page.keyboard.press(key)
    return {"ok": True, "key": key}


def _type_text(page: Any, text: str) -> dict[str, Any]:
    page.keyboard.type(text)
    return {"ok": True, "typed_bytes": len(text.encode("utf-8"))}


def _scroll(page: Any, dx: int, dy: int) -> dict[str, Any]:
    page.mouse.wheel(dx, dy)
    return {"ok": True, "dx": dx, "dy": dy}


def screenshot_bytes(*, run_id: str | None = None,
                     full_page: bool = False) -> tuple[bytes | None, str | None]:
    """``(png_bytes, error)`` for an in-process caller (the ``ui_check`` macro).

    Raw bytes rather than the base64 the ``screenshot`` COMMAND returns: that
    field exists for the agent-facing dispatcher, and round-tripping a
    quarter-megabyte image through base64 only to decode it again is waste.
    """
    if not _playwright_available():
        return None, "playwright_missing"
    rid = _effective_run_id(run_id)
    try:
        _ctx, page = _get_context(rid)
    except Exception as exc:  # noqa: BLE001
        return None, f"browser_launch_failed: {str(exc)[:200]}"
    try:
        return page.screenshot(full_page=bool(full_page)), None
    except Exception as exc:  # noqa: BLE001
        return None, f"screenshot_failed: {str(exc)[:200]}"


# Arguments where an empty string is not a usable value (an address or a name),
# unlike `text`, where "" is a legitimate thing to type.
_NON_EMPTY_ARGS = frozenset({"url", "selector", "key"})


def _missing(args: dict, names: tuple, code: str) -> str | None:
    """``code`` when any required argument is absent, else None.

    The error CODE is given per command rather than derived, because the two
    two-argument commands report a combined one (``missing_x_or_y``,
    ``missing_selector_or_text``) that the model's prompt already names.
    """
    for n in names:
        v = args.get(n)
        if v is None or (n in _NON_EMPTY_ARGS and not v):
            return code
    return None


# command -> (required args, error code, handler taking (page, args))
_BROWSE_COMMANDS = {
    "goto": (("url",), "missing_url", lambda page, a: _goto(page, a["url"])),
    "screenshot": ((), "", lambda page, a: _screenshot(page, a.get("path"),
                                                       a.get("full_page"))),
    "viewport": (("width", "height"), "missing_width_or_height",
                 lambda page, a: _viewport(page, a["width"], a["height"])),
    "wait_for": ((), "", lambda page, a: _wait_for(page, a.get("selector"),
                                                   a.get("state"), a.get("ms"))),
    "click": (("selector",), "missing_selector",
              lambda page, a: _click(page, a["selector"])),
    "fill": (("selector", "text"), "missing_selector_or_text",
             lambda page, a: _fill(page, a["selector"], a["text"])),
    "extract_text": ((), "",
                     lambda page, a: _extract_text(page, a.get("selector"))),
    "mouse_click": (("x", "y"), "missing_x_or_y",
                    lambda page, a: _mouse_click(page, a["x"], a["y"],
                                                 a.get("button") or "left")),
    "key_press": (("key",), "missing_key",
                  lambda page, a: _key_press(page, a["key"])),
    "type": (("text",), "missing_text",
             lambda page, a: _type_text(page, a["text"])),
    "scroll": ((), "", lambda page, a: _scroll(page, a.get("dx") or 0,
                                               a.get("dy") or 0)),
}


# NOSONAR (S107) — this signature IS the tool schema. ADK derives the
# JSON schema the model sees from these parameter names and types; a
# params object or **kwargs here would hand the model an opaque blob
# and it would stop being able to call the tool correctly.
def browse(  # NOSONAR
    command: str,
    *,
    url: str | None = None,
    path: str | None = None,
    selector: str | None = None,
    text: str | None = None,
    x: int | None = None,
    y: int | None = None,
    button: str | None = None,
    key: str | None = None,
    dx: int | None = None,
    dy: int | None = None,
    width: int | None = None,
    height: int | None = None,
    state: str | None = None,
    ms: int | None = None,
    full_page: bool | None = None,
    clear: bool | None = None,
    errors_only: bool | None = None,
    _run_id: str | None = None,
) -> dict[str, Any]:
    """OpenHands-parity browser dispatcher.

    Commands: ``goto``, ``screenshot`` (path/full_page), ``click``, ``fill``,
    ``extract_text``, ``mouse_click`` (x/y/button), ``key_press`` (key),
    ``type`` (text), ``scroll`` (dx/dy), ``viewport`` (width/height),
    ``wait_for`` (selector | state | ms), ``console`` (clear/errors_only),
    ``close``. Soft-error contract.
    """
    if not _playwright_available():
        return {"ok": False, "error": "playwright_missing",
                "hint": "pip install playwright && playwright install chromium"}
    _run_id = _effective_run_id(_run_id)
    if command == "close":
        destroy_context(_run_id)
        return {"ok": True}
    if command == "console":
        # Reads the buffer only — deliberately does NOT create a context, so
        # asking for diagnostics can never launch a browser.
        return _console_cmd(_run_id, True if clear is None else bool(clear),
                            bool(errors_only))
    spec = _BROWSE_COMMANDS.get(command)
    if spec is None:
        return {"ok": False, "error": "unknown_command", "command": command}
    required, code, handler = spec
    args = {"url": url, "path": path, "selector": selector, "text": text,
            "x": x, "y": y, "button": button, "key": key, "dx": dx, "dy": dy,
            "width": width, "height": height, "state": state, "ms": ms,
            "full_page": full_page}
    missing = _missing(args, required, code)
    if missing:
        return {"ok": False, "error": missing}
    # Refuse a disallowed URL BEFORE launching anything. The check also lives
    # in _goto (other callers reach it directly), but doing it only there meant
    # a refused URL still paid for — and leaked — a whole headless Chromium.
    if command == "goto" and not _allowlist_ok(str(url or "")):
        return {"ok": False, "error": "url_not_in_allowlist", "url": url}
    try:
        _ctx, page = _get_context(_run_id)
    except Exception as exc:  # noqa: BLE001 — install/launch failures
        return {"ok": False, "error": "browser_launch_failed",
                "detail": str(exc)[:300]}
    try:
        return handler(page, args)
    except Exception as exc:  # noqa: BLE001
        emit("Browse", {"action": "error", "command": command,
                        "error": str(exc)[:300]})
        return {"ok": False, "error": "browser_op_failed",
                "command": command, "detail": str(exc)[:300]}
