"""Files a shell command wrote get the same checks as an edit.

A model that writes files from the shell (``python -c "open(f,'w')..."``,
``sed -i``, ``cat > f``) skips everything an edit tool triggers: the syntax
and import check, the rewrite guard, the regression watch. more-itertools
(main 312c2e71): at 11 min the hidden score was 5/7; then three
``open('more.py','w')`` scripts added ``'Stats'`` to ``__all__`` without the
class, ``import more_itertools`` failed and the run ended 0/7 — no check saw
it, because no edit tool ran.

After a successful shell command, the Python files whose content changed
since the last look (and that no edit tool wrote with exactly that content)
count as an edit: ``edits_made`` moves (so the regression watch runs) and the
post-edit diagnostics run on them, on this command's result. One
``git status`` per shell command; files git ignores are never looked at.
Off: ``AIFORGE_CHAT_SHELL_WRITE_CHECKS=0``.
"""
from __future__ import annotations

import os

_SHELL = ("run_command", "bash", "shell")


def _on() -> bool:
    return os.environ.get("AIFORGE_CHAT_SHELL_WRITE_CHECKS", "1").strip().lower() not in (
        "0", "false", "no", "off")


def shell_edits(st, name: str, result, cwd: str) -> list:
    """``[(as named, absolute)]`` for the .py files this command changed."""
    if not _on() or name not in _SHELL or not isinstance(result, dict) \
            or result.get("running") or result.get("background"):
        return []
    from ._progress import _tracked_changes
    try:
        now = _tracked_changes(st, cwd)
    except Exception:  # noqa: BLE001
        return []
    if now is None:
        return []
    seen = st.__dict__.get("shell_seen")
    st.shell_seen = dict(now)
    if seen is None:
        return []                       # first look: the baseline
    written = getattr(st, "file_hashes", {}) or {}
    out = []
    for path, digest in now.items():
        if not path.endswith(".py") or not os.path.isfile(path):
            continue
        if seen.get(path) == digest or written.get(path) == digest:
            continue                    # unchanged, or an edit tool wrote exactly this
        out.append((os.path.relpath(path, cwd) if cwd else path, path))
    return out
