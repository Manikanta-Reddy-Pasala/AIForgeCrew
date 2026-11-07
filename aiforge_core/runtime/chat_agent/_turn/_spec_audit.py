"""Before a big request's answer, a fresh read of the spec against the code.

The agent's own tests check the agent's own reading of the request. attrs
n2 (11/18): the spec said ``on_setattr_hook`` is "the effective hook ... e.g.
the default convert+validate pipe that ``define()`` installs"; the code and
its 17 passing tests said ``None``, and 5 hidden tests failed on it. Every
check of the turn passed, because each one asked the agent's reading.

So once per turn, when a big request (a long spec, or three or more parts)
is about to be answered with time left to act on it, one model call with
no history reads the request and the change (the diff of the source, tests
left out) and lists only concrete mismatches: a stated requirement the code
gets wrong or leaves out. Found ones go back to the agent once, as findings
to check, not orders: it fixes the real ones with a test that asserts what
the request states. "NONE", an unclear answer, a failed or slow call, or
Stop let the answer through. ``AIFORGE_CHAT_SPEC_AUDIT=0`` turns it off.
"""
from __future__ import annotations

import contextvars
import os
import re
import subprocess
import threading
import time

from ._shared import _log

#: A request this long, or with this many parts, is a spec worth a read.
_BIG_CHARS, _BIG_PARTS = 1500, 3
#: The request and the change, as much as one call reads.
_SPEC_CHARS, _DIFF_CHARS = 14000, 45000
#: Time the agent needs to act on the findings.
_LEFT_SHARE, _LEFT_MIN_S = 0.12, 300
_SHOWN = 12

_SYSTEM = (
    "You compare a code change with the specification it implements and report "
    "ONLY concrete mismatches you are sure of: a requirement the specification "
    "states in words that the code gets wrong (a different value, type, name, "
    "default, order, error or behaviour) or does not implement at all. Decide "
    "each one before you write it: never write an item you then call fine, "
    "and never write reasoning. Not: style, things the specification does not "
    "state, tests, docs, or code that is not shown.\n"
    "Write each mismatch on one line, exactly in this form:\n"
    "SPEC: \"<the specification's words>\" | CODE: <what the code does instead> "
    "(<file>: <function>)\n"
    "If there is none, reply with the single word NONE.")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_SPEC_AUDIT", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _timeout_s() -> int:
    try:
        return max(10, int(os.environ.get("AIFORGE_CHAT_SPEC_AUDIT_TIMEOUT_S", "180")))
    except ValueError:
        return 180


def _big(st) -> bool:
    goal = str(getattr(st, "goal", "") or "")
    parts = sum(1 for it in (getattr(st, "board", None) or {}).values()
                if isinstance(it, dict) and it.get("from_request"))
    return len(goal) > _BIG_CHARS or parts >= _BIG_PARTS


def _time_left(st) -> bool:
    deadline = getattr(st, "turn_deadline", None)
    budget = getattr(st, "turn_budget_s", 0) or 0
    if deadline is None or not budget:
        return True
    left = deadline - time.monotonic()
    return left >= max(_LEFT_MIN_S, _LEFT_SHARE * budget)


def _applies(st, builder, strict_finish) -> bool:
    return bool(_on() and not builder and not strict_finish
                and not getattr(st, "spec_audited", False)
                and not getattr(st, "readonly_mode", False)
                and not getattr(st, "plan_mode", False)
                and not getattr(st, "goals_cut", False)
                and getattr(st, "edits_made", 0)
                and _big(st) and _time_left(st))


def _git(root: str, *args) -> "tuple[int, str]":
    r = subprocess.run(["git", "--no-optional-locks", "-C", root, *args],
                       capture_output=True, text=True, timeout=30)
    return r.returncode, r.stdout


def change_text(st) -> str:
    """The source change since the work began — the new files whole first
    (often the heart of the work), then the diff of tracked files — tests
    left out, cut at the budget with a marker; "" when git cannot tell."""
    from ._contract_gate import work_base
    from ._example_gate import _is_test_path
    base = work_base(st)
    cwd = str(getattr(st, "cwd", "") or ".")
    if not base:
        return ""
    parts, size, cut = [], 0, False
    try:
        code, root = _git(cwd, "rev-parse", "--show-toplevel")
        root = root.strip()
        if code != 0 or not root:
            return ""
        before = getattr(st, "example_untracked0", None) or set()
        _c, new = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
        for n in new.split("\0"):
            if not n or n in before or _is_test_path(n):
                continue
            if size >= _DIFF_CHARS:
                cut = True
                break
            p = os.path.join(root, n)
            try:
                if os.path.getsize(p) > 60000:
                    continue
                with open(p, encoding="utf-8") as fh:
                    body = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "\0" in body:
                continue                    # binary
            parts.append(f"--- new file: {n}\n{body}")
            size += len(parts[-1])
        code, names = _git(root, "diff", "--name-only", "--no-relative", "-z", base)
        if code != 0:
            return ""
        changed = [n for n in names.split("\0") if n and not _is_test_path(n)][:300]
        if changed and size < _DIFF_CHARS:
            parts.append(_git(root, "diff", "--no-ext-diff", "-U3", base, "--", *changed)[1])
    except Exception:  # noqa: BLE001 — no change text: no audit
        return ""
    text = "\n".join(parts)
    if cut or len(text) > _DIFF_CHARS:
        text = (text[:_DIFF_CHARS] + "\n[change cut here for length — code not shown "
                "is not missing; say nothing about it]")
    return text


_ROW = re.compile(r'^[\W\d]*SPEC\W*\s*(.+?)\s*\|\s*\W*CODE\W*\s*(.+)$', re.I)
_FINE = re.compile(r"\b(?:not a mismatch|no mismatch|this is fine|is fine here|"
                   r"matches the spec)\b", re.I)


def findings(reply: str) -> list:
    """The ``SPEC: … | CODE: …`` lines of the model's reply (anything else,
    a line that calls itself fine, or NONE gives none)."""
    rows = []
    for ln in str(reply or "").splitlines():
        m = _ROW.match(ln.strip())
        if m and not _FINE.search(ln):
            rows.append(f'{m.group(1).strip()} — code: {m.group(2).strip()}'[:400])
    return rows


def audit(role: str, spec: str, change: str, session_id=None, deadline=None) -> list:
    """The mismatches one fresh read finds, or [] (none, unclear, failed,
    slow, or Stop)."""
    ask = (f"SPECIFICATION (the user's request):\n{spec[:_SPEC_CHARS]}\n\n"
           f"CODE CHANGE (diff and new files, tests left out):\n{change}\n\n"
           "List the mismatches, one per line, or reply NONE.")
    box: dict = {}

    def call():
        try:
            from aiforge_core.llm import client, model_wait
            with model_wait.optional():
                box["out"] = client.complete(role, [
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user", "content": ask},
                ], max_tokens=900, temperature=0, timeout_s=_timeout_s())
        except Exception as exc:  # noqa: BLE001 — the audit never blocks an answer
            _log.debug("spec audit failed: %s", exc)

    worker = threading.Thread(target=contextvars.copy_context().run, args=(call,),
                              daemon=True, name="spec-audit")
    worker.start()
    until = time.monotonic() + _timeout_s() + 5
    if deadline is not None:
        until = min(until, deadline - _LEFT_MIN_S / 2)   # leave time to act on it
    while worker.is_alive() and time.monotonic() < until:
        if session_id is not None:
            try:
                from aiforge_core.runtime import chat_cancel
                if chat_cancel.is_cancelled(session_id):
                    return []
            except Exception:  # noqa: BLE001
                pass
        worker.join(0.2)
    return findings(box.get("out") or "")


def message(rows: list) -> str:
    more = f"\n- … and {len(rows) - _SHOWN} more" if len(rows) > _SHOWN else ""
    return ("[automated spec audit — not the user] A fresh read of the request "
            "against your change found these possible mismatches:\n"
            + "\n".join(f"- {r}" for r in rows[:_SHOWN]) + more + "\n"
            "Check each against the request's exact words. Fix the real ones in the "
            "code, with a test asserting what the request states. Change an existing "
            "test only when the request's exact words contradict it; if unsure, leave "
            "the code and the test as they are. Then run the tests and give your "
            "final answer.")


def gate(st, step, builder=None, strict_finish=False):
    """Once per turn: ``"continue"`` with the findings when the fresh read
    finds mismatches."""
    if not _applies(st, builder, strict_finish):
        return None
    st.spec_audited = True
    change = change_text(st)
    if not change.strip():
        return None
    yield {"type": "thought", "role": "system",
           "text": "⧗ reading the request against the change once more…"}
    rows = audit(getattr(st, "role", "chat") or "chat", str(getattr(st, "goal", "") or ""),
                 change, getattr(st, "session_id", None), getattr(st, "turn_deadline", None))
    if not rows:
        return None
    if step.get("text"):
        yield {"type": "thought", "text": step["text"]}
    yield {"type": "thought", "role": "system",
           "text": f"▶ {len(rows)} possible mismatch(es) with the request — checking them"}
    st.convo.append({"role": "user", "content": message(rows)})
    return "continue"


__all__ = ["audit", "change_text", "findings", "gate", "message"]
