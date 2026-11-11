"""After an edit: say at once when it leaves a function as a stub.

One note per new set of stubs in a file (runtime/stubs finds them, any
language); no send-back, no extra round. The turn's facts line repeats the
stubs still left when the turn ends, so a "done" over an empty core is visible
to the user as well. Only stubs this work ADDED count, and test/docs/example
files are left alone (a fake that raises NotImplementedError is fine there).
"""
from __future__ import annotations

import os
import re
import subprocess

_ASIDE = re.compile(r"(?:^|/)(?:tests?|__tests__|spec|specs|testing|docs?|examples?|"
                    r"benchmarks?|fixtures?|mocks?|vendor|_vendor|node_modules|"
                    r"\.worktrees|build|dist|site-packages)/|(?:^|/)(?:test_[^/]*|"
                    r"[^/]*_test\.[a-z]+|[^/]*\.(?:test|spec)\.[a-z]+|conftest\.py)$")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_STUB_NOTE", "1").strip().lower() not in (
        "0", "false", "no", "off")


def _git(args, cwd, timeout=4):
    try:
        return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                              text=True, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None


def _top(path: str) -> "str | None":
    r = _git(["rev-parse", "--show-toplevel"], os.path.dirname(path) or ".")
    return r.stdout.strip() if r is not None and r.returncode == 0 else None


def _text_at(base: "str | None", top: str, rel: str) -> "str | None":
    """The file at the work's base commit; None when it did not exist."""
    if not base:
        return None
    r = _git(["show", f"{base}:{rel}"], top)
    return r.stdout if r is not None and r.returncode == 0 else None


def _new_in(full: str, base: "str | None") -> list:
    from aiforge_core.runtime import stubs
    top = _top(full)
    if not top or not os.path.isfile(full):
        return []
    rel = os.path.relpath(os.path.realpath(full), os.path.realpath(top))
    if _ASIDE.search(rel.replace(os.sep, "/")):
        return []
    try:
        with open(full, encoding="utf-8", errors="replace") as fh:
            now = fh.read()
    except OSError:
        return []
    return stubs.new_stubs(full, _text_at(base, top, rel), now)


def _phrase(found: list) -> str:
    return "; ".join(f"`{n}` (line {ln}) {why}" for n, ln, why in found[:6])


def check(st, files, cwd):
    """Yield a chat line and hold a note for the model when the files just
    written gained stubs. Said once per file per distinct set of stubs."""
    if not _on():
        return
    from ._contract_gate import work_base
    base = work_base(st)
    said = st.__dict__.setdefault("stub_said", {}) if hasattr(st, "__dict__") else {}
    for shown, full in files:
        try:
            found = _new_in(full, base)
        except Exception:  # noqa: BLE001 — a check never breaks a step
            continue
        key = tuple(sorted(n for n, _, _ in found))
        if not found:
            said.pop(full, None)
            continue
        if said.get(full) == key:
            continue
        said[full] = key
        yield {"type": "thought", "role": "system",
               "text": f"⚠ {shown} has {len(found)} unfinished stub(s): "
                       f"{_phrase(found)[:200]}"}
        from aiforge_core.runtime import stubs
        from ._notes import hold
        lang = stubs.hint(full)
        hold(st, "[automated stub check — not the user] After this edit, "
                 f"{shown} has functions with no real body: {_phrase(found)}. "
                 + (lang + " " if lang else "")
                 + "The request still needs this code to work: write the real "
                 "body before moving on to other parts; if you leave it "
                 "unfinished, say so plainly in your answer.")


def stubs_left(cwd: str, head0: "str | None") -> str:
    """Facts-line phrase: stubs the turn's changed files still have that the
    start of the work did not ("" when none or unknown)."""
    if not _on() or not head0 or not cwd:
        return ""
    top = _top(os.path.join(cwd, "."))
    if not top:
        return ""
    names: list = []
    diff = _git(["diff", "--name-only", head0], top)
    new = _git(["ls-files", "--others", "--exclude-standard"], top)
    rels = set()
    for r in (diff, new):
        if r is not None and r.returncode == 0:
            rels.update(x for x in r.stdout.split("\n") if x.strip())
    for rel in sorted(rels)[:200]:
        full = os.path.join(top, rel)
        try:
            names += [f"{os.path.basename(rel)}:{n}" for n, _, _ in _new_in(full, head0)]
        except Exception:  # noqa: BLE001
            continue
    if not names:
        return ""
    shown = ", ".join(f"`{n}`" for n in names[:5])
    more = f" (+{len(names) - 5})" if len(names) > 5 else ""
    return f"unfinished stubs (not implemented): {shown}{more}"


__all__ = ["check", "stubs_left"]
