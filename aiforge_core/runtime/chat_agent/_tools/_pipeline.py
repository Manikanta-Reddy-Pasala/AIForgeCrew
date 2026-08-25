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


def _t_browse(args: dict, cwd: str) -> dict:
    try:
        from aiforge_core.runtime.tools.browser import browse
        return browse(str(args.get("command") or ""),
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
                      _run_id=_chat_run_id(cwd))
    except Exception as exc:  # noqa: BLE001 — playwright may be absent
        return {"ok": False, "error": str(exc)}


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
