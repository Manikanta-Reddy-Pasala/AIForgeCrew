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


def set_run_id(run_id: str | None) -> None:
    _RUN_ID.set(run_id)


def _effective_run_id(explicit: str | None) -> str:
    return explicit or _RUN_ID.get() or "default"

_SCREENSHOT_CAP_BYTES = 256 * 1024
_TEXT_CAP_BYTES = 32 * 1024

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


def _allowlist_ok(url: str) -> bool:
    from urllib.parse import urlsplit

    host = (urlsplit(url).hostname or "").lower()

    raw = os.environ.get("AIFORGE_BROWSER_ALLOWLIST", "").strip()
    if raw:
        # Match each allowlist entry against the HOSTNAME with exact-or-
        # subdomain semantics — NOT ``re.search`` over the whole URL, which
        # let ``http://169.254.169.254/#github.com`` match a ``github.com``
        # entry (SSRF to cloud IMDS via a fragment). A host the operator
        # EXPLICITLY allowlisted is trusted even if it's a LAN/loopback dev
        # server — that is the intended use of the browser tool.
        for pattern in raw.split(","):
            pattern = pattern.strip().lower()
            if not pattern:
                continue
            if host == pattern or host.endswith("." + pattern):
                return True
        return False

    # Empty allowlist under the network lockdown: arbitrary browsing only when
    # the operator opts in via AIFORGE_ALLOW_WEB_FETCH=1 — and even then the
    # target is SSRF-guarded so a model-chosen URL can't pivot to cloud
    # metadata / a private LAN host. (A pure DNS failure falls through to the
    # browser, which will just fail to connect — it can't be an SSRF target.)
    if str(os.environ.get("AIFORGE_ALLOW_WEB_FETCH", "0")).strip().lower() not in (
        "1", "true", "yes", "on",
    ):
        return False
    from aiforge_core.net.ssl import SSRFBlocked, guard_public_url
    try:
        guard_public_url(url)
    except SSRFBlocked as exc:
        if exc.kind != "dns":
            return False
    return True


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
    except Exception:
        _teardown_globals()
        raise
    _contexts[run_id] = (ctx, page)
    emit("Browse", {"action": "context_created", "run_id": run_id})
    return _contexts[run_id]


def destroy_context(run_id: str) -> None:
    """Close BrowserContext + cleanup global handles when last run is gone."""
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


def _screenshot(page: Any, path: str | None) -> dict[str, Any]:
    png = page.screenshot(full_page=False)
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
    "screenshot": ((), "", lambda page, a: _screenshot(page, a.get("path"))),
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


def browse(
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
    _run_id: str | None = None,
) -> dict[str, Any]:
    """OpenHands-parity browser dispatcher.

    Commands: ``goto``, ``screenshot``, ``click``, ``fill``, ``extract_text``,
    ``mouse_click`` (x/y/button), ``key_press`` (key), ``type`` (text),
    ``scroll`` (dx/dy), ``close``. Soft-error contract.
    """
    if not _playwright_available():
        return {"ok": False, "error": "playwright_missing",
                "hint": "pip install playwright && playwright install chromium"}
    _run_id = _effective_run_id(_run_id)
    if command == "close":
        destroy_context(_run_id)
        return {"ok": True}
    spec = _BROWSE_COMMANDS.get(command)
    if spec is None:
        return {"ok": False, "error": "unknown_command", "command": command}
    required, code, handler = spec
    args = {"url": url, "path": path, "selector": selector, "text": text,
            "x": x, "y": y, "button": button, "key": key, "dx": dx, "dy": dy}
    missing = _missing(args, required, code)
    if missing:
        return {"ok": False, "error": missing}
    try:
        _ctx, page = _get_context(_run_id)
    except Exception as exc:  # noqa: BLE001 — install/launch failures
        return {"ok": False, "error": "browser_launch_failed",
                "detail": str(exc)[:300]}
    try:
        return handler(page, args)
    except Exception as exc:  # noqa: BLE001 — Playwright timeouts, etc.
        emit("Browse", {"action": "error", "command": command,
                        "error": str(exc)[:300]})
        return {"ok": False, "error": "browser_op_failed",
                "command": command, "detail": str(exc)[:300]}
