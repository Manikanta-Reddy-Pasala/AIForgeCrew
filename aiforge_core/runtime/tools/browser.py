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
import os
import re
import uuid
from typing import Any

from aiforge_core.runtime.sandbox import resolve_inside_root

from ._trace import emit

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
    raw = os.environ.get("AIFORGE_BROWSER_ALLOWLIST", "").strip()
    if not raw:
        return True
    for pattern in raw.split(","):
        pattern = pattern.strip()
        if not pattern:
            continue
        if re.search(pattern, url):
            return True
    return False


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

    if _run_id is None:
        _run_id = "default-" + uuid.uuid4().hex[:8]

    if command == "close":
        destroy_context(_run_id)
        return {"ok": True}

    try:
        _ctx, page = _get_context(_run_id)
    except Exception as exc:  # noqa: BLE001 — install/launch failures
        return {"ok": False, "error": "browser_launch_failed",
                "detail": str(exc)[:300]}

    try:
        if command == "goto":
            if not url:
                return {"ok": False, "error": "missing_url"}
            return _goto(page, url)
        if command == "screenshot":
            return _screenshot(page, path)
        if command == "click":
            if not selector:
                return {"ok": False, "error": "missing_selector"}
            return _click(page, selector)
        if command == "fill":
            if selector is None or text is None:
                return {"ok": False, "error": "missing_selector_or_text"}
            return _fill(page, selector, text)
        if command == "extract_text":
            return _extract_text(page, selector)
        if command == "mouse_click":
            if x is None or y is None:
                return {"ok": False, "error": "missing_x_or_y"}
            return _mouse_click(page, x, y, button or "left")
        if command == "key_press":
            if not key:
                return {"ok": False, "error": "missing_key"}
            return _key_press(page, key)
        if command == "type":
            if text is None:
                return {"ok": False, "error": "missing_text"}
            return _type_text(page, text)
        if command == "scroll":
            return _scroll(page, dx or 0, dy or 0)
        return {"ok": False, "error": "unknown_command", "command": command}
    except Exception as exc:  # noqa: BLE001 — Playwright timeouts, etc.
        emit("Browse", {"action": "error", "command": command,
                        "error": str(exc)[:300]})
        return {"ok": False, "error": "browser_op_failed",
                "command": command, "detail": str(exc)[:300]}
