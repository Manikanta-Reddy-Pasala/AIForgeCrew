"""Evidence for a finished task: what the run DID while the item was open.

When the model marks a board item done (or failed), the user should not have
to ask "show me the evidence". The harness keeps one record of every action
of the turn — the commands with their exit codes and the end of their output,
test runs, edits, reads — and when an item closes it pins the actions taken
since that item started, beside the item's own ``check:`` (the condition it
was meant to meet). Actions no item claims go under the answer itself. The UI
shows both.

This is the harness's record, not the model's say-so: an item closed with no
action since it started says so ("nothing was run or changed"); a failure in
its window is always shown and counted, however many actions came after; an
item that is opened again loses the evidence of its earlier close.
"""
from __future__ import annotations

import re
import time

#: Actions shown per item (failures are always kept on top of these).
MAX_STEPS = 12
#: Actions kept for the whole turn.
MAX_ROWS = 400
#: Output kept per command (its end matters most).
TAIL_CHARS = 1200
#: Tools that are bookkeeping, not work.
_SKIP = frozenset({"plan_progress", "tool_help", "think", "session_actions"})
_COMMIT_TOOLS = frozenset({"git_commit", "git_add_commit", "commit",
                           "github_pr", "gitlab_mr_create"})
_JOB_TOOLS = frozenset({"command_wait", "command_output", "command_kill"})
_CHECK_RE = re.compile(r"\s[—–-]{1,2}\s*check:\s*(.+)$", re.I)


def _edit_tools() -> frozenset:
    from aiforge_core.runtime.tools.mutating import FILE_WRITE_TOOLS
    return frozenset(FILE_WRITE_TOOLS) | {"multi_edit", "format",
                                          "rename_symbol"}


def _command_tools() -> frozenset:
    from aiforge_core.runtime.tools.tool_policy import _CMD_TOOLS
    return frozenset(_CMD_TOOLS) | _JOB_TOOLS | {
        "run_tests", "typecheck", "project", "execute_ipython_cell", "run"}


def check_of(title: str) -> str:
    """The ``— check: ...`` part of an item title, or ""."""
    m = _CHECK_RE.search(title or "")
    return m.group(1).strip()[:300] if m else ""


def _tail(text, n: int = TAIL_CHARS) -> str:
    text = str(text or "").rstrip()
    return text if len(text) <= n else "…" + text[-n:]


def _what(name: str, args: dict) -> str:
    a = args if isinstance(args, dict) else {}
    keys = ("path", "file", "cmd", "command", "code", "pattern", "query",
            "message", "id", "mode", "action", "url", "name")
    for key in keys:
        if a.get(key) not in (None, ""):
            return " ".join(str(a[key]).split())[:300]
    return ""


def _kind(name: str, args) -> str:
    if name in _COMMIT_TOOLS:
        return "edit"
    if name in _edit_tools():
        if name == "editor":
            from aiforge_core.runtime.tools.mutating import EDITOR_READONLY_CMDS
            cmd = str((args or {}).get("command") or "") if isinstance(args, dict) else ""
            if cmd in EDITOR_READONLY_CMDS:
                return "read"
        return "edit"
    if name in _command_tools() or name.startswith("run_"):
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
    kind = _kind(name, args)
    ok = res.get("ok")
    row = {"tool": name, "kind": kind, "what": _what(name, args),
           "ok": None if ok is None else bool(ok), "at": round(time.time(), 3)}
    code = res.get("code", res.get("exit_code"))
    if code is not None:
        row["code"] = code
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


def _rows(st) -> list:
    rows = getattr(st, "evidence_rows", None)
    if rows is None:
        rows = st.evidence_rows = []
    return rows


def _seq(st) -> int:
    """How many actions the turn has taken (the next row's number)."""
    return int(getattr(st, "evidence_seq", 0) or 0)


def note(st, name: str, args, result) -> None:
    """Record this tool call for the turn. Never raises."""
    try:
        row = record(name, args, result)
        if row is None:
            return
        row["seq"] = _seq(st)
        st.evidence_seq = row["seq"] + 1
        rows = _rows(st)
        rows.append(row)
        if len(rows) > MAX_ROWS:
            _trim(st, rows)
        st.evidence_last = None          # a tool ran: no longer a joint close
    except Exception:  # noqa: BLE001 — evidence must never break a turn
        pass


def _trim(st, rows: list) -> None:
    """Drop the oldest actions that did NOT fail (a failure is always kept),
    remembering which were dropped so a window can say so."""
    over = len(rows) - MAX_ROWS
    drop = set()
    for r in rows:
        if len(drop) >= over:
            break
        if not _failed(r):
            drop.add(id(r))
    lost = getattr(st, "evidence_lost", None)
    if lost is None:
        lost = st.evidence_lost = []
    lost.extend(r["seq"] for r in rows if id(r) in drop)
    del lost[:-5000]
    rows[:] = [r for r in rows if id(r) not in drop]


def _lost_since(st, start: int, claimed_too: bool = True) -> int:
    claimed = getattr(st, "evidence_claimed", None) or set()
    return sum(1 for q in (getattr(st, "evidence_lost", None) or ())
               if q >= start and (claimed_too or q not in claimed))


def _failed(r: dict) -> bool:
    if r.get("running"):
        return False
    code = r.get("code")
    return r.get("ok") is False or (code is not None and code != 0)


def _pick(rows: list) -> list:
    """What to show: every failure, then commands and edits (newest), then
    reads to fill up — at most MAX_STEPS besides the failures — in the order
    they ran."""
    bad = [r for r in rows if _failed(r)]
    strong = [r for r in rows if r["kind"] in ("command", "edit") and not _failed(r)]
    room = max(0, MAX_STEPS - len(bad))
    strong = strong[-room:] if room else []
    room -= len(strong)
    weak = ([r for r in rows if r["kind"] not in ("command", "edit")
             and not _failed(r)][-room:] if room > 0 else [])
    keep = {id(r) for r in bad + strong + weak}
    return [r for r in rows if id(r) in keep]


def _last_outcome(cmds: list) -> str:
    """How the last command that has a verdict ended: its exit code, else
    ok/failed. Peeks at a job (no code, nothing proven) are skipped."""
    for r in reversed(cmds):
        if r.get("running"):
            return "still running"
        if r.get("code") is not None:
            return f"exit {r['code']}"
        if r["tool"] not in _JOB_TOOLS and r.get("ok") is not None:
            return "ok" if r["ok"] else "failed"
    return ""


def _build(rows: list, check: str, status: str, lost: int = 0) -> dict:
    """The evidence for ``rows``. Counts and failures are over ALL of them,
    not only the steps shown; ``lost`` older actions (none failed) were not
    kept in a very long turn."""
    steps = _pick(rows)
    ev = {"check": check, "steps": steps,
          "closed_at": round(time.time(), 3), "status": status}
    if len(steps) < len(rows) or lost:
        ev["shown_of"] = [len(steps), len(rows) + lost]
    if lost:
        ev["lost"] = lost
    if not rows and not lost:
        ev["note"] = "nothing was run or changed while this item was open"
        return ev
    cmds = [r for r in rows if r["kind"] == "command"]
    edits = [r for r in rows if r["kind"] == "edit"]
    failed = [r for r in rows if _failed(r)]
    bits = []
    if cmds:
        last = _last_outcome(cmds)
        bits.append(f"{len(cmds)} command{'s' * (len(cmds) != 1)}"
                    + (f", last {last}" if last else ""))
    if edits:
        bits.append(f"{len(edits)} edit{'s' * (len(edits) != 1)}")
    if failed:
        bits.append(f"{len(failed)} failed")
    ev["summary"] = " · ".join(bits) or f"{len(rows)} lookups"
    return ev


def _claim(st, rows: list) -> None:
    claimed = getattr(st, "evidence_claimed", None)
    if claimed is None:
        claimed = st.evidence_claimed = set()
    claimed.update(r["seq"] for r in rows)


def forget(item: dict) -> None:
    """An item opened again: the evidence of its earlier close no longer
    stands."""
    item.pop("evidence", None)
    item.pop("_ev_from", None)


def on_status(st, slug: str, before: str | None, after: str) -> dict | None:
    """An item changed status. Starting one (running) marks where its record
    begins; reopening one drops its old evidence; closing one (done / failed)
    pins the actions since it started and returns that evidence."""
    board = getattr(st, "board", None) or {}
    item = board.get(slug)
    if item is None or before == after:
        return None
    if after not in ("done", "failed"):
        if before in ("done", "failed"):
            forget(item)
        if after == "running":
            item["_ev_from"] = _seq(st)
            st.evidence_last = None
        return None
    start = item.get("_ev_from")
    if start is None:
        start = int(getattr(st, "evidence_mark", 0) or 0)
    rows = [r for r in _rows(st) if r["seq"] >= start]
    shared = None
    last = getattr(st, "evidence_last", None)
    if not rows and last and last[0] != slug:
        # Closed in the same breath as the item before it (no tool call in
        # between): the work that proved that one is the work behind this.
        shared, rows = last[0], list(last[1])
    lost = 0 if shared else _lost_since(st, start)
    ev = _build(rows, check_of(item.get("title", "")), after, lost)
    if shared:
        ev["shared_with"] = shared
    _claim(st, rows)
    st.evidence_mark = _seq(st)
    st.evidence_last = (slug, rows) if rows else None
    if item.get("note"):                 # the item verifier's verdict (_items)
        ev["verdict"] = str(item["note"])[:320]
    if item.get("files"):
        ev["files"] = list(item["files"])[:20]
    item.pop("_ev_from", None)
    item["evidence"] = ev
    return ev


def _unclaimed(st) -> list:
    claimed = getattr(st, "evidence_claimed", None) or set()
    return [r for r in _rows(st) if r["seq"] not in claimed]


def for_answer(st) -> dict | None:
    """Evidence for the final answer itself: what the turn did that no closed
    task claims (the whole turn when no task closed). None when there is
    nothing left to show (a plain answer, or every action is under a task)."""
    rows = _unclaimed(st)
    if not rows:
        return None
    return _build(rows, "", "done")


def for_parts(st, slugs) -> list:
    """Parts of the request the harness closes at FINAL (the model never
    marked them). The first gets the turn's unclaimed work; the others say
    they share it. Returns ``[(slug, evidence)]``."""
    board = getattr(st, "board", None) or {}
    rows = _unclaimed(st)
    out = []
    first = None
    for slug in slugs:
        item = board.get(slug)
        if item is None or item.get("evidence"):
            continue
        ev = _build(rows, check_of(item.get("title", "")), "done")
        if first is None:
            first = slug
            _claim(st, rows)
        elif rows:
            ev["shared_with"] = first
        item["evidence"] = ev
        out.append((slug, ev))
    return out


__all__ = ["MAX_STEPS", "check_of", "for_answer", "for_parts", "forget",
           "note", "on_status", "record"]
