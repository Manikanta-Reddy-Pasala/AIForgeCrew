"""The browse tool's commands: navigation, screenshots, waiting, console, input
and text extraction."""
from __future__ import annotations

import base64
from typing import Any

from aiforge_core.runtime.sandbox import resolve_inside_root


def _pkg():
    """``browser``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``browser``; patch any other
    name on this module."""
    import aiforge_core.runtime.tools.browser as package
    return package


def _goto(page: Any, url: str) -> dict[str, Any]:
    if not _pkg()._allowlist_ok(url):
        return {"ok": False, "error": "url_not_in_allowlist", "url": url}
    response = page.goto(url, timeout=30000)
    status = response.status if response else None
    return {
        "ok": True, "url": page.url, "title": page.title(), "status": status,
    }


def _screenshot(page: Any, path: str | None,
                full_page: bool = False) -> dict[str, Any]:
    pkg = _pkg()
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
    b64 = base64.b64encode(png[:pkg._SCREENSHOT_CAP_BYTES]).decode("ascii")
    return {
        "ok": True, "path": out_path,
        "png_b64": b64, "bytes": len(png),
        "truncated": len(png) > pkg._SCREENSHOT_CAP_BYTES,
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
    entries = _pkg().drain_console(run_id, clear=clear, errors_only=errors_only)
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
    pkg = _pkg()
    if selector:
        text = page.inner_text(selector, timeout=10000)
    else:
        text = page.inner_text("body", timeout=10000)
    if len(text.encode("utf-8")) > pkg._TEXT_CAP_BYTES:
        text = text.encode("utf-8")[:pkg._TEXT_CAP_BYTES].decode("utf-8", "replace")
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
    pkg = _pkg()
    if not pkg._playwright_available():
        return None, "playwright_missing"
    rid = pkg._effective_run_id(run_id)
    try:
        _ctx, page = pkg._get_context(rid)
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
