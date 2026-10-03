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
#: A go-ahead ("yes continue") already gave the permission: three firm reminders.
_GO_AHEAD_NUDGES = 3


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
        self.forced = False               # a go-ahead / admitted stall (see detect)
        self._go_ahead = False
        self._wants = False

    def applies(self, st) -> bool:
        if (self.readonly_mode or self.plan_mode or self.builder
                or st.edits_made != 0 or not _no_change_guard_enabled()):
            return False
        from aiforge_core.runtime.chat_router import wants_changes

        from .._turn import _goahead
        goal = getattr(st, "goal", "") or ""
        # "yes continue" refers to the request before it: hold the turn to that one.
        self._go_ahead = (_goahead.is_go_ahead(goal)
                          and _goahead.had_earlier_assistant_turn(st))
        self._wants = bool(wants_changes(
            _goahead.effective_goal(st) if self._go_ahead else goal))
        return self._wants or self._go_ahead

    def budget(self, st) -> int:
        if self.forced:
            return _GO_AHEAD_NUDGES
        return _NO_CHANGE_NUDGES if _bigger_task(st, self.asks) else 0

    def detect(self, text: str) -> list:
        from .._turn import _goahead
        text = (text or "").strip()
        # A change request whose answer admits nothing was done, or that the work
        # comes "next turn", is a stall like a go-ahead that gets no work.
        self.forced = self._go_ahead or (self._wants and _goahead.admits_no_work(text))
        if not text:
            return []
        # A final that ASKS the user something is left alone — except one that
        # asks for the go-ahead the user has just given.
        if text.endswith("?") and not (self.forced and _goahead.asks_permission(text)):
            return []
        return [text]

    def evidence(self, st) -> bool:
        now = _worktree_fingerprint(self.cwd)
        return now == "" or now != self.wt_fp0    # no git signal, or it changed

    def on_nudge(self, st) -> None:
        if self.forced:
            from .._turn._escalate import arm_reasoning
            arm_reasoning(st)             # a stalled step is the one worth thinking about

    def notice(self, claims) -> str:
        if self.forced:
            return ("⚠ you were told to go ahead but no file has been edited "
                    "— doing the work…")
        return ("⚠ this request asks for changes but no file has been "
                "edited — doing the work…")

    def nudge(self, claims) -> str:
        if self.forced:
            from .._turn import _goahead
            return _goahead.NUDGE
        return ("[harness — not the user] The request asks for CHANGES, and you "
                "have edited NO file. A plan, review or list of what remains is "
                "not the result. Implement it now: make the edits, run the "
                "checks, then give a FINAL that says what changed. If you have "
                "verified that nothing needs to change, say so with the "
                "evidence (the file and line that already does it).")

    def disclaimer(self, claims) -> str:
        return ("(No file was changed in this turn — what follows is "
                "analysis or a plan, not an implementation.)\n\n")
