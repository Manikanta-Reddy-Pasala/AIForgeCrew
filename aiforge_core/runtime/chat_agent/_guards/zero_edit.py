"""A turn that is about to end with no file changed: the WORKING model is asked
once, in its own context, whether the user's message wanted work.

The harness used to decide that itself from the wording of the message (a
go-ahead opener, a list of change verbs). Every live miss was a message the
word rules could not read: "its ok continue simplfying all files .. use kiss"
was neither a go-ahead nor a change request, so "Nothing was written this
turn" was accepted as the result. The model that is doing the work has the
whole conversation; a rule has one sentence.

So, on a FINAL with no landed edit AND an unchanged tree (the disk is checked,
not the model's word), the model gets one harness note: read the user's last
message with the conversation before it — if it asked for work, do it now; if
it asked a question, say so. Its next reply decides: tool calls continue the
work; otherwise the turn ends with its answer.

* The check fires at most once per turn.
* A model that reads the message as a question answers the check with the one
  word ``SAME``: the answer it had already written is sent as it is. (Live,
  without that word, a model re-answered with "You asked a question, not for a
  change. Nothing to do" — a reply to the harness where the user's answer
  should have been.)
* It is skipped in plan / analyze / builder runs, and for an obvious question
  (the message ends with '?' or opens like a question, and names no change) —
  the word rules are used for that: to SAVE the extra step, never to decide
  that work is required.
* A model that answers the check by admitting it did nothing ("nothing was
  written", "in the next turn", asking for a go-ahead it already has) gets the
  firm reminder, up to three times; after that the answer is labelled so a
  plan is never read as the result. The reminder asserts "you were told to go
  ahead", so it needs the user's own words to say so (a go-ahead or a named
  change): it is never sent on the model's admission alone.
* A FINAL that says the change could not be made (it begins with
  ``NOT DONE:``) is labelled the same way.
"""
from __future__ import annotations

import os
import re

from .file_edit import _worktree_fingerprint

#: Firm reminders for a model that admits it did nothing after the check.
_GO_AHEAD_NUDGES = 3

CHECK = (
    "[harness — not the user] A routine check before this turn ends. The user "
    "does not see this note. No file was changed in this turn.\n"
    "That is correct when the user's last message was a question, a remark, "
    "a complaint, or something your answer above already settles. Then reply "
    "with the single word SAME: your answer above is sent to the user "
    "unchanged. Do not redo, undo or edit anything, and do not explain. When "
    "you are not sure that the user asked for file changes, reply SAME.\n"
    "Only if the user's last message, read together with the conversation "
    "before it, clearly asked you to do, continue or change something in the "
    "files, and that work is not done: do it now with tool calls. A go-ahead "
    "the user already gave is not to be asked for again, and a plan or a "
    "list of what remains is not the result.\n"
    "So reply with either the single word SAME, or the tool calls that do "
    "the missing work. Two exceptions: if you need something from the user "
    "that the conversation does not give, ask for it as your FINAL; if a "
    "requested change cannot be made, begin your FINAL with 'NOT DONE:' and "
    "say exactly what blocks you.")

#: The model's "my previous answer stands" (see CHECK): the word alone, with
#: what a model wraps around a one-word reply, or closing a line of reasoning
#: ("…that was a question, nothing was left undone. SAME").
_SAME_END = re.compile(r"(?:^|[\s.:;—-])SAME[\s.!*_`'\")\]]*\Z")
#: The reply is the word in any case, alone (with what a model wraps around a
#: one-word reply) …
_SAME_WHOLE = re.compile(
    r"\A[\s*_`'\"(\[]*(?:final\s*:\s*)?same[\s.!*_`'\")\]]*\Z", re.I)
#: … or it OPENS with the word exactly as the check asks for it, in capitals,
#: then explains ("SAME — it was a question."). "Same-origin policy blocks…" and
#: "Same: the function returns…" are answers that merely begin with the word.
_SAME_START = re.compile(
    r"\A[\s*_`'\"(\[]*(?:(?:FINAL|Final|final)\s*:\s*)?SAME(?:\s*\Z|\s*[.!:,;—\-*_`'\")\]])")


_ABOUT_THE_USER = re.compile(
    r"\bthe\s+user(?:'s)?\b|\buser's\s+(?:message|request|question|last)\b"
    r"|\bthe\s+(?:request|message|question)\s+(?:was|is|only|did|does)\b"
    r"|\bthe\s+(?:harness|action\s+log)\b|\bthe\s+note\s+(?:is|was|above)\b", re.I)


def _to_the_harness(text: str) -> bool:
    """A reply that talks ABOUT the user rather than TO them."""
    return bool(_ABOUT_THE_USER.search(text or ""))


def _tool_calls(st) -> int:
    try:
        return sum(int(v or 0) for v in (getattr(st, "action_counts", None) or {}).values())
    except Exception:  # noqa: BLE001
        return 0


def _says_same(text: str) -> bool:
    return bool(_SAME_WHOLE.match(text) or _SAME_START.match(text)
                or _SAME_END.search(text))


_NOT_DONE = re.compile(r"^[\s*_#>`]*not\s+done\b[\s*_:`—-]*", re.I)

DISCLAIMER = ("(No file was changed in this turn — what follows is "
              "analysis or a plan, not an implementation.)\n\n")


def _no_change_guard_enabled() -> bool:
    """AIFORGE_CHAT_NO_CHANGE_GUARD=0 turns the zero-edit check off."""
    return os.environ.get("AIFORGE_CHAT_NO_CHANGE_GUARD", "1").strip().lower() \
        not in ("0", "false", "no", "off")


def head_commit(cwd) -> "str | None":
    """The commit the tree is on, or None when there is no git signal (not a
    repository, no commit yet, git missing)."""
    if not cwd:
        return None
    try:
        import subprocess
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=cwd,
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except Exception:  # noqa: BLE001 — a git hiccup must never break the turn
        return None


def _reads_as_question(goal: str) -> bool:
    """The message reads as a question (it ends with '?', or opens with "how",
    "what", "explain", "tell me" …), names no change and is not a go-ahead. A
    cheap filter that only ever SAVES the check — it never decides that work
    is required.

    Live, the check on such a message did harm: asked "tell me which functions
    calc.py defines", the model answered, got the note, and went back to work
    for twenty more tool calls."""
    from aiforge_core.runtime.chat_router import is_advice_question, wants_changes

    from .._turn._goahead import is_go_ahead
    g = (goal or "").strip()
    return bool(g) and is_advice_question(g) and not wants_changes(g) \
        and not is_go_ahead(g)


def _small_talk(goal: str) -> bool:
    """"thanks", "hi": nothing to do. Not a go-ahead ("ok", "yes")."""
    from .._turn._goahead import is_go_ahead
    try:
        from aiforge_core.runtime.parallel_subtasks import _is_trivial_prompt
        return bool(_is_trivial_prompt(goal)) and not is_go_ahead(goal)
    except Exception:  # noqa: BLE001
        return False


class ZeroEditGuard:
    counter = "no_change_nudges"

    def __init__(self, cwd, readonly_mode, builder, plan_mode, asks, wt_fp0,
                 strict: bool = False):
        self.cwd, self.readonly_mode = cwd, readonly_mode
        self.builder, self.plan_mode = builder, plan_mode
        self.asks, self.wt_fp0 = asks, wt_fp0
        self.strict = strict

    def applies(self, st) -> bool:
        """An act-mode turn of a CHAT that has landed no edit. Not a work-producing
        run (the pipeline Doer, a subtask, a scheduled job): its "user message" is
        a seed the harness wrote, its finals are refused unless explicit (a bare
        SAME would cost two more rounds), and the pipeline has its own no-edit
        rule."""
        return not (self.strict or self.readonly_mode or self.plan_mode or self.builder
                    or st.edits_made != 0 or not _no_change_guard_enabled())

    def evidence(self, st) -> bool:
        """True when the disk shows work, or cannot be asked. The dirty state
        changed, or — in a tree that is clean before and after (a chat's
        workspace is committed at the start of every turn, so that is the
        normal case) — the commit it is on changed. No git: no verdict."""
        now = _worktree_fingerprint(self.cwd)
        if now != self.wt_fp0:
            return True
        if now:
            return False                  # the same dirty tree as at the start
        head = head_commit(self.cwd)
        return head is None or head != getattr(st, "head0", None)

    def _answered_directly(self, st) -> bool:
        """An obvious question: the answer stands and no extra model step is
        spent on it, whatever the model looked up to answer it. Small talk
        ("thanks") too, when no tool ran at all."""
        goal = getattr(st, "goal", "") or ""
        if _reads_as_question(goal):
            return True
        return not getattr(st, "action_counts", None) and _small_talk(goal)

    def _stalled(self, st, text: str) -> bool:
        """After the check the model still says it did nothing, or asks for a
        go-ahead the user's message already was.

        The firm reminder tells the model, as a fact, that it was told to go
        ahead — so it is sent only when the user's own words say so (a
        go-ahead, or a named change). Anywhere else "nothing was changed" can
        BE the answer ("when did you commit" → "nothing was committed in this
        turn"), and pushing the model to edit would be the harness inventing
        a request."""
        from aiforge_core.runtime.chat_router import wants_changes

        from .._turn import _goahead
        goal = getattr(st, "goal", "") or ""
        go = _goahead.is_go_ahead(goal)
        if not (go or wants_changes(goal)):
            return False
        return bool(_goahead.admits_no_work(text)
                    or (go and _goahead.asks_permission(text)))

    def _send_back(self, st, step, notice: str, nudge: str, echo: bool = True):
        if echo and step.get("text"):
            yield {"type": "thought", "text": step["text"]}
        yield {"type": "thought", "role": "system", "text": notice}
        st.convo.append({"role": "user", "content": nudge})
        return "continue"

    def check(self, st, step):
        """Run by :func:`..base.run_guards` on a FINAL step. Returns
        ``"continue"`` when the model is sent back, else None."""
        from .._turn import _goahead
        text = (step.get("text") or "").strip()
        if not text or not self.applies(st) or self.evidence(st):
            return None
        if not getattr(st, "zero_edit_checked", False):
            # A final that asks the user something is left alone — unless the
            # user's message was a go-ahead, or what the final asks for is one
            # (the model then reads, in context, whether it already has it).
            if text.endswith("?") and not (
                    _goahead.asks_permission(text)
                    or _goahead.is_go_ahead(getattr(st, "goal", "") or "")):
                return None
            if self._answered_directly(st):
                return None
            st.zero_edit_checked = True
            st.zero_edit_answer = step.get("text") or ""     # sent on "SAME"
            st.zero_edit_tools = _tool_calls(st)
            return (yield from self._send_back(
                st, step, "↻ no file was changed — checking that against what "
                          "you asked…", CHECK, echo=False))
        if _says_same(text):
            # Read as a question: the answer written for the user stands —
            # unless that answer was itself the stall ("nothing was written,
            # say start"), which "SAME" does not turn into a result.
            text = (getattr(st, "zero_edit_answer", "") or text).strip()
            step["text"] = text
            if not self._stalled(st, text):
                return None
        elif _NOT_DONE.match(text):
            step["text"] = DISCLAIMER + _NOT_DONE.sub("", text, count=1).strip()
            return None
        elif not self._stalled(st, text):
            # The model's new answer stands (a correction, or "nothing needs to
            # change, because …") — except a reply that ANSWERS THE CHECK: no tool
            # ran since, and it speaks ABOUT the user in the third person ("the
            # user only asked to run the tests; no file changes were needed").
            # That is addressed to the harness; the answer written for the user
            # is the one from before the check.
            before = (getattr(st, "zero_edit_answer", "") or "").strip()
            if (before and not text.endswith("?") and _to_the_harness(text)
                    and _tool_calls(st) == getattr(st, "zero_edit_tools", -1)):
                step["text"] = before
            return None
        sent = getattr(st, self.counter, 0)
        if sent < _GO_AHEAD_NUDGES:
            setattr(st, self.counter, sent + 1)
            from .._turn._escalate import arm_reasoning
            arm_reasoning(st)             # a stalled step is the one worth thinking about
            return (yield from self._send_back(
                st, step, "⚠ you were told to go ahead but no file has been "
                          "edited — doing the work…", _goahead.NUDGE))
        step["text"] = DISCLAIMER + text
        return None
