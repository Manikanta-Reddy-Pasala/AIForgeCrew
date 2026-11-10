from __future__ import annotations

from ._shared import _chat_run_id, _coerce_int


# --- Pipeline-parity tools: mcp, browser, jupyter, sub-agent delegate -------
# The team pipeline Doer has these four; the SIMPLE-CHAT agent now matches it
# (and Claude Code / Cursor, which expose browser + MCP + sub-agents in a single
# agent). All degrade soft: if the dep (playwright / jupyter_client) or import
# is unavailable, the handler returns {"ok": False, "error": ...} instead of
# raising into the chat loop.
def _t_mcp(args: dict, _cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.mcp_client import mcp
        return mcp(str(args.get("command") or ""),
                   endpoint=args.get("endpoint"),
                   tool=args.get("tool"),
                   arguments=args.get("arguments"))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _describe_screenshot(result: dict, args: dict,
                         run_id: str | None = None) -> dict:
    """Replace a screenshot's base64 payload with something the model can USE.

    ``browse`` returns ``png_b64`` because its own contract is byte-level — raw
    base64 is an unreadable 340KB observation. The image is stored, then:

    * when the chat model is multimodal, the next OBSERVATION carries the
      pixels on the same turn (no second vision call by default);
    * otherwise a separate vision model captions it (text-only path).

    Pass ``audit=false`` to skip the caption call; pass ``audit=true`` to
    force a caption even when the chat model can already see the pixels.
    """
    from aiforge_core.runtime import visual
    from aiforge_core.runtime.chat_agent._obs_vision import (
        chat_can_see, mark_pixels,
    )

    png_b64 = result.pop("png_b64", None)
    if not result.get("ok") or not png_b64:
        return result
    import base64
    if result.get("truncated"):
        # The base64 is only a PREFIX of the PNG (browse caps it at 256 KB), so
        # decoding it yields a corrupt image — and any full-page shot of a real
        # app exceeds that. Re-take the capture as raw bytes rather than hand
        # the model nothing, which is what dropping it here would do.
        from aiforge_core.runtime.tools.browser import screenshot_bytes
        raw, err = screenshot_bytes(run_id=run_id)
        if raw is None:
            result["note"] = ("image too large to return inline and could not "
                              f"be re-captured ({err})")
            return result
    else:
        try:
            raw = base64.b64decode(png_b64)
        except Exception:  # noqa: BLE001
            return result
    capture_id, path = visual.save_capture(raw, "browse")
    if path is None:
        result["note"] = ("screenshot taken but not stored — check permissions "
                          "on $AIFORGE_CONFIG_DIR/captures")
        return result
    result["capture_id"] = capture_id
    result["screenshot"] = path
    can_see = chat_can_see(role="chat")
    if can_see:
        mark_pixels(result, path, role="chat")
        result["see"] = True
        # Default: attach pixels, skip the second-model caption.
        if str(args.get("audit", "false")).lower() not in ("1", "true", "yes"):
            return result
    elif str(args.get("audit", "true")).lower() in ("0", "false", "no"):
        return result
    audit = visual.audit_image(path, role="chat")
    if audit.get("ok"):
        result["audit"] = audit["text"]
        result["vision_role"] = audit.get("vision_role")
    else:
        result["audit_error"] = audit.get("error")
        result["audit_hint"] = audit.get("hint") or audit.get("detail")
    return result


def _t_browse(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.browser import browse
        command = str(args.get("command") or args.get("action") or "")
        result = browse(command,
                        url=args.get("url"),
                        path=args.get("path"),
                        selector=args.get("selector"),
                        text=args.get("text"),
                        x=_coerce_int(args.get("x")),
                        y=_coerce_int(args.get("y")),
                        button=args.get("button"),
                        key=args.get("key"),
                        dx=_coerce_int(args.get("dx")),
                        dy=_coerce_int(args.get("dy")),
                        width=_coerce_int(args.get("width")),
                        height=_coerce_int(args.get("height")),
                        state=args.get("state"),
                        ms=_coerce_int(args.get("ms")),
                        full_page=bool(args.get("full_page")),
                        clear=args.get("clear"),
                        errors_only=bool(args.get("errors_only")),
                        _run_id=_chat_run_id(cwd))
        if command == "screenshot" and isinstance(result, dict):
            return _describe_screenshot(result, args, _chat_run_id(cwd))
        return result
    except Exception as exc:  # noqa: BLE001 — playwright may be absent
        return {"ok": False, "error": str(exc)}


def _t_ui_check(args: dict, cwd: str) -> dict:
    """Start-or-reuse the app, open a page, screenshot it, and report what a
    vision model sees plus the page's own console/network errors.

    Shares this chat's browser context with ``browse``: a follow-up click, or
    ``browse console``, must act on the page ui_check just loaded rather than
    on a fresh about:blank in a different context.
    """
    try:
        from aiforge_core.runtime import visual
        return visual.ui_check(args, cwd, run_id=_chat_run_id(cwd))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "ui_check_failed", "detail": str(exc)[:300]}


def _t_ui_ask(args: dict, cwd: str) -> dict:
    """Ask the vision model a follow-up question about a stored capture."""
    try:
        from aiforge_core.runtime import visual
        return visual.ui_ask(args, cwd)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": "ui_ask_failed", "detail": str(exc)[:300]}


def _t_ipython(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.ipython_kernel import execute_ipython_cell
        kwargs: dict = {"_run_id": _chat_run_id(cwd)}
        _timeout = _coerce_int(args.get("timeout"))
        if _timeout is not None:
            kwargs["timeout"] = _timeout
        return execute_ipython_cell(str(args.get("code") or ""), **kwargs)
    except Exception as exc:  # noqa: BLE001 — jupyter_client may be absent
        return {"ok": False, "error": str(exc)}


def _t_delegate(args: dict, _cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.delegation import delegate_to_agent
        kwargs: dict = {}
        _timeout = _coerce_int(args.get("timeout"))
        if _timeout is not None:
            kwargs["timeout"] = _timeout
        return delegate_to_agent(str(args.get("role") or ""),
                                 str(args.get("prompt") or ""), **kwargs)
    except Exception as exc:  # noqa: BLE001 — soft-fail
        return {"ok": False, "error": str(exc)}
