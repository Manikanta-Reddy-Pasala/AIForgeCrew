"""Running a tool call: lookup, unknown-tool results, and shown/secret
argument handling."""
from __future__ import annotations

import time

from .._registry import (
    TOOLS,
    _perf_family,
)
from .._tools import (
    _ROOT_SCOPED_TOOLS,
    _scoped_root,
)


def _invoke_tool(fn, name, args, cwd):
    """Run one tool fn under a scoped sandbox-root override (reset in finally)
    with perf recording; KeyError/Exception become an error result."""
    _perf_t0 = time.perf_counter()
    # Strong tools resolve through sandbox.root(); scope the override to
    # the workspace root (NOT the raw cwd, so it can't escape an
    # AIFORGE_WORKSPACE_DIR jail) and ALWAYS reset it in finally so a
    # reused thread can't leak this session's dir into the next.
    _root_tok = None
    if name in _ROOT_SCOPED_TOOLS:
        try:
            from aiforge_core.runtime import sandbox as _sb
            _root_tok = _sb.set_root_override(_scoped_root(cwd))
        except Exception:  # noqa: BLE001
            _root_tok = None
    try:
        result = fn(args, cwd)
    except KeyError as exc:
        result = {"ok": False, "error": f"missing arg: {exc}"}
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": str(exc)}
    finally:
        if _root_tok is not None:
            try:
                from aiforge_core.runtime import sandbox as _sb
                _sb.reset_root_override(_root_tok)
            except Exception:  # noqa: BLE001
                pass
    try:
        from aiforge_core.runtime import perf_recorder
        perf_recorder.record(
            _perf_family(name), name,
            (time.perf_counter() - _perf_t0) * 1000.0)
    except Exception:  # noqa: BLE001 — perf must never break a run
        pass
    return result


# Names a model reaches for when it wants to search. The capability was
# removed (the query string is outbound data), and a bare "unknown tool" sends
# the model round the alias carousel — web_search, search_web, websearch, google
# — burning steps before it gives up and often answering from memory anyway.
# Say what happened and what to do instead.
_REMOVED_SEARCH_TOOLS = frozenset({
    "web_search", "websearch", "search_web", "web_query", "google",
    "google_search", "duckduckgo", "ddg_search", "internet_search",
    "search_internet", "bing_search", "brave_search", "tavily_search",
})


def _unknown_tool_result(name) -> dict:
    if str(name).strip().lower().replace("-", "_") in _REMOVED_SEARCH_TOOLS:
        return {"ok": False, "error": "web_search_removed",
                "hint": ("this install has no web search, by design — the "
                         "query text is outbound data. Do not try another "
                         "name for it, and do not fetch a search engine's URL "
                         "either. Use what you can read here, or ask the user "
                         "for a direct URL / the content itself.")}
    return {"ok": False, "error": f"unknown tool: {name}"}


def _dispatch_tool(name, args, cwd, n, _hook_block):
    """Dispatch one tool call: honour a PreToolUse hook block / unknown tool,
    else emit ``tool_start`` and run ``fn(args, cwd)`` under a scoped sandbox
    root override (reset in finally) with perf recording. Returns the result
    dict."""
    fn = TOOLS.get(name)
    if _hook_block is not None:
        result = {"ok": False, "blocked": "hook", "hook": _hook_block,
                  "error": f"'{name}' was blocked by a PreToolUse hook"}
    elif fn is None:
        result = _unknown_tool_result(name)
    else:
        # Live "it's running" signal — a slow tool (bash/test/build) used
        # to show NOTHING until `fn` returned, so the UI looked stalled
        # for however long the command actually took. `call_id` (the
        # ReAct step counter `n`, unique per iteration) lets the UI match
        # this to the completed `tool` event below and flip it in place
        # instead of appending a second, duplicate row.
        yield {"type": "tool_start", "name": name,
               "args": _shown_args(name, args), "call_id": n}
        result = _invoke_tool(fn, name, args, cwd)
    return result


# Argument values never shown in a step (the UI, the replay buffer, the saved
# turn): save_secret's value. Masked in a COPY before the event leaves — the
# tool masks its own args too, but only once it runs, after tool_start was out.
_SECRET_ARGS = {"save_secret": ("value",)}


def _shown_args(name, args):
    keys = _SECRET_ARGS.get(name)
    if not keys or not isinstance(args, dict):
        return args
    return {k: ("[secret]" if k in keys else v) for k, v in args.items()}


_SHELL_TOOLS = ("run_command", "bash", "run_shell", "shell", "serve",
                "watch_until", "ui_check")


def _is_destructive_delete(cmd: str, cwd: "str | None" = None) -> bool:
    """Whether ``cmd`` deletes, unless the env opt-in already allows deletes."""
    try:
        from aiforge_core.runtime.tools import delete_guard
        return (not delete_guard.allow_delete(
            ("AIFORGE_CHAT_ALLOW_DELETE", "AIFORGE_ALLOW_DELETE"))
            and delete_guard.is_destructive_delete(cmd, cwd))
    except Exception:  # noqa: BLE001
        return False
