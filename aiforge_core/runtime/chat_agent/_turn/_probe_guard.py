"""Shell commands in a row with no change to the code: a model probing
instead of working.

On a 5-feature benchmark the agent ran 101 shell commands in a row (and
streaks of 37 and 32) without one edit: throwaway `python3 -c "..."` scripts
trying the code, cat/grep through the shell instead of the read tools. The
same model under OpenCode read, wrote large edits and ran the tests. Every
``EVERY`` commands without a landed edit the observation says so. Not in a
read-only turn (it may not write), and worded so an ops turn that only runs
commands is not pushed to write code.
Off switch: ``AIFORGE_CHAT_PROBE_GUARD=0``.
"""
from __future__ import annotations

import os

#: Shell commands in a row (no landed edit between) before each note.
EVERY = 10


def _shell_tools() -> frozenset:
    from aiforge_core.runtime.shell_writes import SHELL_TOOLS
    return SHELL_TOOLS


def _writes_through_shell(args, cwd) -> bool:
    try:
        from aiforge_core.runtime.shell_writes import command_of, shell_write_targets
        return bool(shell_write_targets(command_of(args) or "", str(cwd or ".")))
    except Exception:  # noqa: BLE001
        return False


def note(st, name: str, args) -> str:
    """The line to add to this observation, or ""."""
    if os.environ.get("AIFORGE_CHAT_PROBE_GUARD", "1").strip().lower() in ("0", "false", "no", "off"):
        return ""
    if any(getattr(st, k, False) for k in ("readonly_mode", "plan_mode", "analyze_mode")):
        return ""
    edits = getattr(st, "edits_made", 0)
    if edits != getattr(st, "probe_edits_seen", 0):
        # an edit LANDED since the last step (a refused one does not count)
        st.probe_edits_seen = edits
        st.shell_streak = 0
    if name not in _shell_tools():
        return ""
    if _writes_through_shell(args, getattr(st, "cwd", None)):
        st.shell_streak = 0          # sed -i / cat > f: a change, made the shell way
        return ""
    st.shell_streak = getattr(st, "shell_streak", 0) + 1
    if st.shell_streak % EVERY:
        return ""
    return (f"[harness — not the user] {st.shell_streak} shell commands in a row "
            "and no change to the files. If the task needs a code change, stop "
            "probing with throwaway scripts and cat/grep: read what you need with "
            "the read tools, write the change (file_write / multi_edit), then run "
            "the repo's tests.")


__all__ = ["EVERY", "note"]
