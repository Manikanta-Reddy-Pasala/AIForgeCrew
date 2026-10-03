"""Zero-edit FINAL for a request that asked for changes.

The single-agent twin of the team run that ends with a plan. The file-edit
claim guard only catches a final that CLAIMS edits; a plan, review or "the
real work is (a)(b)(c)" claims nothing and was accepted as the finished result.

Checks the disk, not the model's word: no landed edit AND an unchanged tree. A
bigger task gets one reminder to do the work (no other model call is added);
whatever the outcome, an accepted zero-edit final is labelled so a plan is
never read as the result. A final that asks the user something is left alone.
"""
from __future__ import annotations

import os

from .file_edit import _worktree_fingerprint

#: Reminders a zero-edit FINAL gets for a change request.
_NO_CHANGE_NUDGES = 1


def _no_change_guard_enabled() -> bool:
    """AIFORGE_CHAT_NO_CHANGE_GUARD=0 turns the zero-edit check off."""
    return os.environ.get("AIFORGE_CHAT_NO_CHANGE_GUARD", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def _bigger_task(st, _asks) -> bool:
    """A multi-part, long or planned request — the kind where ending with a plan
    instead of the work is likeliest. A short single ask is "small": it gets the
    honest note but never an extra model turn."""
    from aiforge_core.runtime.chat_router import _SMALL_MAX_CHARS, is_small_task
    goal = getattr(st, "goal", "") or ""
    if is_small_task(goal):
        return False
    return bool(_asks or getattr(st, "board_used", False)
                or len(goal) >= _SMALL_MAX_CHARS)


class ZeroEditGuard:
    counter = "no_change_nudges"
    strip_body = True
    echo_text = True

    def __init__(self, cwd, readonly_mode, builder, plan_mode, asks, wt_fp0):
        self.cwd, self.readonly_mode = cwd, readonly_mode
        self.builder, self.plan_mode = builder, plan_mode
        self.asks, self.wt_fp0 = asks, wt_fp0

    def applies(self, st) -> bool:
        if (self.readonly_mode or self.plan_mode or self.builder
                or st.edits_made != 0 or not _no_change_guard_enabled()):
            return False
        from aiforge_core.runtime.chat_router import wants_changes
        return bool(wants_changes(getattr(st, "goal", "") or ""))

    def budget(self, st) -> int:
        return _NO_CHANGE_NUDGES if _bigger_task(st, self.asks) else 0

    def detect(self, text: str) -> list:
        text = (text or "").strip()
        if not text or text.endswith("?"):
            return []                     # asking the user, or nothing to label
        return [text]

    def evidence(self, st) -> bool:
        now = _worktree_fingerprint(self.cwd)
        return now == "" or now != self.wt_fp0    # no git signal, or it changed

    def notice(self, claims) -> str:
        return ("⚠ this request asks for changes but no file has been "
                "edited — doing the work…")

    def nudge(self, claims) -> str:
        return ("[harness — not the user] The request asks for CHANGES, and you "
                "have edited NO file. A plan, review or list of what remains is "
                "not the result. Implement it now: make the edits, run the "
                "checks, then give a FINAL that says what changed. If you have "
                "verified that nothing needs to change, say so with the "
                "evidence (the file and line that already does it).")

    def disclaimer(self, claims) -> str:
        return ("(No file was changed in this turn — what follows is "
                "analysis or a plan, not an implementation.)\n\n")
