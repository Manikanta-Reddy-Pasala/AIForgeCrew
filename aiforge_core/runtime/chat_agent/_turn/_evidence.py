"""Evidence for a finished task: what the run DID while the item was open.

When the model marks a board item done (or failed), the user should not have
to ask "show me the evidence". The harness keeps a short record of every
action since the item started — the commands with their exit codes and the
end of their output, test runs, edits, reads — and pins it to the item at the
moment it closes, beside the item's own ``check:`` (the condition it was
meant to meet). The UI shows it under the item.

This is the harness's record, not the model's say-so: an item closed with no
action since it started says so ("nothing was run or changed").
"""
from __future__ import annotations

import re
import time

#: Actions kept per open item (the newest win).
MAX_STEPS = 12
#: Output kept per command (its end matters most).
TAIL_CHARS = 1200
#: Tools that are bookkeeping, not work.
_SKIP = frozenset({"plan_progress", "tool_help", "think", "session_actions"})
_EDITS = frozenset({"file_patch", "file_write", "file_create", "multi_edit",
                    "editor", "format", "rename_symbol"})
_COMMANDS = frozenset({"run_command", "run_tests", "command_wait",
                       "command_output", "command_kill", "typecheck",
                       "project", "bash", "run_shell", "execute_ipython_cell",
                       "serve", "watch_until"})
_CHECK_RE = re.compile(r"\s[—–-]{1,2}\s*check:\s*(.+)$", re.I)


def check_of(title: str) -> str:
    """The ``— check: ...`` part of an item title, or ""."""
    m = _CHECK_RE.search(title or "")
    return m.group(1).strip()[:300] if m else ""


def _tail(text, n: int = TAIL_CHARS) -> str:
    text = str(text or "").rstrip()
    return text if len(text) <= n else "…" + text[-n:]


def _what(name: str, args: dict) -> str:
    a = args if isinstance(args, dict) else {}
    for key in ("cmd", "command", "code", "path", "file", "pattern", "query",
                "id", "mode", "action", "url", "name"):
        if a.get(key) not in (None, ""):
            return " ".join(str(a[key]).split())[:300]
    return ""


def _kind(name: str) -> str:
    if name in _EDITS:
        return "edit"
    if name in _COMMANDS or name.startswith("run_"):
        return "command"
    return "read" if name.startswith(("file_read", "read_", "list_", "grep",
                                      "find", "git_", "repo_")) else "tool"


def _output(result: dict) -> str:
    parts = []
    for key in ("new_output", "stdout", "output", "summary"):
        if result.get(key):
            parts.append(str(result[key]))
            break
    if result.get("stderr"):
        parts.append("[stderr]\n" + str(result["stderr"]))
    if not parts and result.get("error"):
        parts.append(str(result["error"]))
    return _tail("\n".join(parts))


def record(name: str, args, result) -> dict | None:
    """One compact evidence line for a tool call, or None for bookkeeping."""
    if name in _SKIP:
        return None
    res = result if isinstance(result, dict) else {"output": str(result)}
    kind = _kind(name)
    ok = res.get("ok")
    row = {"tool": name, "kind": kind, "what": _what(name, args),
           "ok": None if ok is None else bool(ok), "at": round(time.time(), 3)}
    if res.get("code") is not None:
        row["code"] = res.get("code")
    if res.get("running"):
        row["running"] = True
    if kind == "command":
        out = _output(res)
        if out:
            row["output"] = out
    elif ok is False and res.get("error"):
        row["output"] = _tail(res.get("error"), 400)
    for key in ("passed", "failed", "errors", "tests"):
        if isinstance(res.get(key), (int, float)):
            row[key] = res[key]
    return row


def _buf(st) -> list:
    buf = getattr(st, "evidence_buf", None)
    if buf is None:
        buf = []
        st.evidence_buf = buf
    return buf


def note(st, name: str, args, result) -> None:
    """Keep this tool call for the item that closes next. Never raises."""
    try:
        row = record(name, args, result)
        if row is None:
            return
        buf = _buf(st)
        buf.append(row)
        st.evidence_last = None          # a tool ran: no longer a joint close
        if len(buf) > MAX_STEPS * 4:
            del buf[:-MAX_STEPS * 4]
    except Exception:  # noqa: BLE001 — evidence must never break a turn
        pass


def _pick(rows: list) -> list:
    """The steps worth showing: every command and edit (newest last), then
    reads to fill up, at most MAX_STEPS, in the order they ran."""
    strong = [r for r in rows if r["kind"] in ("command", "edit")]
    if len(strong) >= MAX_STEPS:
        return strong[-MAX_STEPS:]
    room = MAX_STEPS - len(strong)
    weak = [r for r in rows if r["kind"] not in ("command", "edit")][-room:]
    keep = {id(r) for r in strong + weak}
    return [r for r in rows if id(r) in keep]


def on_status(st, slug: str, before: str | None, after: str) -> dict | None:
    """An item changed status. Starting one (running) begins its record;
    closing one (done / failed) pins the record to it and returns it."""
    board = getattr(st, "board", None) or {}
    item = board.get(slug)
    if item is None or before == after:
        return None
    if after == "running":
        st.evidence_buf = []
        return None
    if after not in ("done", "failed"):
        return None
    rows = list(_buf(st))
    st.evidence_buf = []
    steps = _pick(rows)
    shared = None
    last = getattr(st, "evidence_last", None)
    if not steps and last and last[0] != slug:
        # Closed in the same breath as the item before it (no tool call in
        # between): the work that proved that one is the work behind this.
        shared, steps = last[0], list(last[1])
    ev = {"check": check_of(item.get("title", "")), "steps": steps,
          "closed_at": round(time.time(), 3), "status": after}
    if shared:
        ev["shared_with"] = shared
    st.evidence_last = (slug, steps) if steps else None
    if item.get("note"):                 # the item verifier's verdict (_items)
        ev["verdict"] = str(item["note"])[:320]
    if item.get("files"):
        ev["files"] = list(item["files"])[:20]
    if not steps:
        ev["note"] = "nothing was run or changed while this item was open"
    else:
        failed = [s for s in steps if s.get("ok") is False]
        ran = [s for s in steps if s["kind"] == "command"]
        edits = [s for s in steps if s["kind"] == "edit"]
        bits = []
        if ran:
            last = ran[-1]
            bits.append(f"{len(ran)} command{'s' * (len(ran) != 1)}, last "
                        + ("still running" if last.get("running") else
                           f"exit {last.get('code')}" if last.get("code") is not None
                           else ("ok" if last.get("ok") else "failed")))
        if edits:
            bits.append(f"{len(edits)} edit{'s' * (len(edits) != 1)}")
        if failed:
            bits.append(f"{len(failed)} failed")
        ev["summary"] = " · ".join(bits) or f"{len(steps)} lookups"
    item["evidence"] = ev
    return ev


__all__ = ["MAX_STEPS", "check_of", "note", "on_status", "record"]
