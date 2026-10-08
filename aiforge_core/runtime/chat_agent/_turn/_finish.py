"""Handling the model's non-tool replies: finals (verify-on-final, false-claim
guard, final nudges, turn summary, next-step suggestion) and narration
without an action."""
from __future__ import annotations

import contextlib
import contextvars
import os
import re
import subprocess
import threading
import time

from .._context import (
    _fire_stop,
    _repo_name,
    _text_of,
)
from .._guards import (
    EchoGuard,
    ExternalClaimGuard,
    FileEditClaimGuard,
    UnchangedFileClaimGuard,
    UnpassedCheckGuard,
    ZeroEditGuard,
    run_guards,
)
from .._prompt import _strip_reasoning_prefix
from .._registry import (
    _BUILDER_FINALIZE_TOOL,
)
from ._escalate import pause_on_stuck
from ._stuck.detect import detect
from ._stuck.ladder import change_approach
from ._outcomes import _verify_on_final
from ._shared import (
    _THE_FINALIZE_TOOL,
    _log,
)
from ._tasks import board_nudge_allowed, open_for_final, unfinished_reminder


def _final_nudges(st, step, builder, strict_finish, _asks):
    """Pre-accept FINAL nudges: builder-not-finalized reminder, implicit-final
    doer nudge (strict_finish), and the one-time multi-ask completeness gate.
    Returns continue to loop again, or None to proceed."""
    # A reply that is only the system's `[did: …]` action log is not an answer
    # (strip it, make the model do the work); "I created the Confluence page"
    # with no successful Confluence/Jira call this turn is not true (make it
    # call the tool, or say so).
    _sig = yield from run_guards(st, step, [EchoGuard(),
                                            ExternalClaimGuard(builder)])
    if _sig == "continue":
        return "continue"
    # In a builder session, a "final" BEFORE the finalize tool succeeded
    # means the model narrated/stalled ("let me test what's happening…")
    # instead of building the artifact — don't end the interview with
    # nothing created. Nudge it to call the finalize tool and continue the
    # loop (bounded so a model that truly can't finalize still exits).
    if builder and not st.builder_finalized and st.builder_final_tries < 2:
        st.builder_final_tries += 1
        _fin = _BUILDER_FINALIZE_TOOL.get(builder, _THE_FINALIZE_TOOL)
        if step.get("text"):
            yield {"type": "thought", "text": step["text"]}
        st.convo.append({"role": "user", "content":
            f"[system reminder] You stopped without creating the {builder}. "
            f"Call `{_fin}` NOW with the collected values to finish — do "
            f"not just narrate or 'test'. If ONE required value is genuinely "
            f"missing, ask only for that, then finalize."})
        return "continue"
    # Doer guard: an IMPLICIT final (bare prose, no explicit `FINAL:`
    # marker) from a work-producing run (strict_finish — the text-doer /
    # subtask path) is almost always premature narration ("let me test…"),
    # not a real answer. Nudge to act/finish instead of ending with no work.
    # Bounded by continue_nudges so a model that truly can't finish still
    # exits. Interactive chat / generic callers (strict_finish=False) keep
    # bare prose as the legitimate answer — unchanged.
    if step.get("implicit") and strict_finish and not builder:
        st.continue_nudges += 1
        if st.continue_nudges <= 2:
            if step.get("text"):
                yield {"type": "thought", "text": step["text"]}
            st.convo.append({"role": "user", "content":
                "You narrated but did NOT emit an ACTION or an explicit "
                "`FINAL:` line. Continue: take the next ACTION (tool call) "
                "to make progress, or output `FINAL: <answer>` ONLY when "
                "the work is actually done. Do not just narrate or 'test'."})
            return "continue"
    # A command this turn handed back is still running: an answer now would
    # report its output so far as the result (live: "batch 29/40" given as
    # the end of a 40-batch job). One nudge; the model may still answer —
    # the jobs are then promoted to background jobs (_handle_final), so
    # "still running" stays true after the turn ends.
    if not getattr(st, "running_job_nudged", False):
        try:
            from aiforge_core.runtime import cmd_jobs
            live = cmd_jobs.turn_running()
        except Exception:  # noqa: BLE001 — never block an answer on this
            live = []
        if live:
            st.running_job_nudged = True
            ids = ", ".join(str(j.key) for j in live)
            if step.get("text"):
                yield {"type": "thought", "text": step["text"]}
            st.convo.append({"role": "user", "content":
                f"[harness — not the user] Command(s) {ids} you started are "
                "STILL RUNNING, so their output so far is not the result. "
                "command_wait for them before answering, or command_kill "
                "them — or, if the answer does not depend on them, answer "
                "now: they will keep running as background jobs (Stop ends "
                "them) and their result is posted to this chat when they "
                "finish. Say plainly that they are still running."})
            return "continue"
    _sig = yield from _wait_for_own_jobs(st, step)
    if _sig:
        return _sig
    _sig = yield from _not_from_a_toolless_reply(st, step, builder)
    if _sig:
        return _sig
    _sig = yield from _do_what_you_said(st, step, builder, strict_finish)
    if _sig:
        return _sig
    # Task board gate: the model planned items and some are still open. A
    # long run must not stop to report half the work; bounded so a model
    # that cannot finish still exits.
    _left = open_for_final(st)
    if ((st.board_used or getattr(st, "board_touched", False)) and _left
            and not builder and not st.readonly_mode and board_nudge_allowed(st)):
        if step.get("text"):
            yield {"type": "thought", "text": step["text"]}
        yield {"type": "thought", "role": "system",
               "text": f"☐ {len(_left)} task(s) still open — continuing"}
        st.convo.append({"role": "user",
                         "content": unfinished_reminder(st.board, _left)})
        return "continue"
    # A finished answer is the answer. The task board above is the check,
    # and it does not ask the model to resend the same text as FINAL.
    return None


def _is_clean_tree(cwd) -> bool:
    """True only when ``cwd`` IS a git repo AND has nothing uncommitted.

    Deliberately NOT ``_worktree_fingerprint(cwd) == ""``: that helper returns
    "" for a clean tree *and* for "not a git repo / git unavailable", and its
    own docstring warns that "" means "no signal", never "clean". Reusing it
    here would let a tier-2 prediction act automatically in a directory with no
    undo at all — precisely the case the clean-tree rule exists to exclude.

    Anything unclear answers False, which costs an offer instead of an action.
    """
    if not cwd:
        return False
    try:
        # --no-optional-locks: this runs on a side thread while the next turn
        # may already be writing; a status must never take index.lock.
        out = subprocess.run(["git", "--no-optional-locks", "status",
                              "--porcelain"], cwd=str(cwd),
                             capture_output=True, text=True, timeout=5)
    except Exception:  # noqa: BLE001 — a git hiccup must never break the turn
        return False
    return out.returncode == 0 and not out.stdout.strip()


def _turn_summary(st) -> str:
    """One line naming what this turn actually did, for the prediction prompt.

    Read off ``action_counts``, which the loop already maintains — the
    alternative is a second tally that drifts from the first.
    """
    try:
        counts = getattr(st, "action_counts", None) or {}
        names = list(dict.fromkeys(str(k).split("|", 1)[0]
                                   for k, v in counts.items() if v))
    except Exception:  # noqa: BLE001
        return ""
    return ", ".join(names[:8])


def _last_user_message(st) -> str:
    try:
        for m in reversed(list(getattr(st, "convo", []) or [])):
            if isinstance(m, dict) and m.get("role") == "user":
                # _text_of takes the whole MESSAGE, not its content: it handles
                # the multimodal list form a vision turn rewrites content into.
                return _text_of(m)[:2000]
    except Exception:  # noqa: BLE001 — a malformed convo predicts nothing
        return ""
    return ""


def _predict_next_step(message: str, did: str, cwd):
    """The prediction, or None. Split out so a test can replace exactly this.

    The kill switch is honoured BEFORE the context is gathered. ``_is_clean_tree``
    shells out to the VCS on every turn end, so building the argument first meant
    a disabled feature still paid for a subprocess per turn — "off" has to mean
    it costs nothing, not merely that it emits nothing.

    Not recorded here (``store=False``): ``_collect_suggestion`` records only
    the prediction it actually emits.
    """
    from aiforge_core.runtime import next_step
    from aiforge_core.runtime.next_step import _predict as _np

    if _np._disabled():
        return None
    return next_step.predict({"message": message, "did": did,
                              "repo": _repo_name(str(cwd or "")),
                              "clean_tree": _is_clean_tree(cwd)}, store=False)


def _suggest_grace_s() -> float:
    """How long the end of a turn waits, AFTER ``done``, for the prediction
    before dropping it (see ``_suggest_wait``: default 3 s). The prediction
    started beside the answer, so it has had the whole answer's time too."""
    from ._suggest_wait import after_done_grace_s
    return after_done_grace_s()


def _start_suggestion(message: str, did: str, cwd):
    """Start the prediction (LLM call + clean-tree probe) on a side thread, so
    it overlaps the answer being streamed instead of holding ``done`` back.
    Returns a handle for ``_collect_suggestion``, or None when disabled.

    The call is bound to its own cancel token, set when the prediction is
    dropped, so an abandoned request does not keep a single-slot local model
    busy into the next turn."""
    from aiforge_core.runtime.next_step import _predict as _np

    if _np._disabled():
        return None
    ready, cancel, box = threading.Event(), threading.Event(), {}

    def _run():
        try:
            with contextlib.suppress(Exception):  # uncancellable is still correct
                from aiforge_core.llm import client as _client
                _client.set_cancel_event(cancel)
            box["p"] = _predict_next_step(message, did, cwd)
        except Exception as exc:  # noqa: BLE001 — a prediction never breaks a turn
            _log.debug("next_step: prediction skipped: %s", exc)
        finally:
            ready.set()

    # copy_context: the LLM call is metered/attributed via contextvars.
    threading.Thread(target=contextvars.copy_context().run, args=(_run,),
                     name="aiforge-next-step", daemon=True).start()
    return ready, cancel, box, message, cwd, time.monotonic()


def _cancel_suggestion(handle) -> None:
    """Abort the prediction's in-flight call (a no-op once it has finished)."""
    if handle is not None:
        handle[1].set()


def _collect_suggestion(handle, session_id=None):
    """Yield at most one ``suggestion`` event: the prediction if it is ready
    within the grace, else nothing. Never raises. A late prediction is
    cancelled and dropped unrecorded — the user never saw it, so it must not
    suppress a repeat. Stop or a new message ends the wait at once."""
    if handle is None:
        return
    try:
        yield from _ready_suggestion(handle, session_id)
    finally:
        _cancel_suggestion(handle)


def _ready_suggestion(handle, session_id=None):
    from ._suggest_wait import await_ready
    ready, _cancel, box, message, cwd, t0 = handle
    grace = _suggest_grace_s()
    if not await_ready(ready, session_id, grace):
        # info, not debug: a grace the model never meets is a dead feature,
        # and it should be visible as one.
        _log.info("next_step: suggestion dropped — not ready %.1fs after the "
                  "answer (grace %.1fs)", time.monotonic() - t0, grace)
        return
    p = box.get("p")
    if p is None:
        return
    try:
        from aiforge_core.runtime import next_step
        next_step.remember(p, {"message": message,
                               "repo": _repo_name(str(cwd or ""))})
        ev = p.as_event()
    except Exception as exc:  # noqa: BLE001 — a prediction never breaks a turn
        _log.debug("next_step: suggestion skipped: %s", exc)
        return
    yield ev


def _endpoint_one_slot() -> bool:
    """The prediction would queue behind chat on a one-slot server (see
    ``llm/slots.py``; unknown is one slot). The extra call is then skipped."""
    try:
        from aiforge_core.llm import slots
        role = os.environ.get("AIFORGE_PREDICT_ROLE", "enhancer")
        return not slots.parallel_ok(role, slots.CHAT_ROLE)
    except Exception:  # noqa: BLE001
        return True


def _emit_suggestion(message: str, did: str, cwd):
    """Yield at most one ``suggestion`` event. Never raises.

    Emitted AFTER ``done``. A prediction that is slow, wrong or broken costs
    the user nothing: it is waited for at most ``AIFORGE_PREDICT_AFTER_DONE_S``
    (default 3 s), then dropped.
    """
    yield from _collect_suggestion(_start_suggestion(message, did, cwd))


def _handle_final(st, step, builder, strict_finish, plan_mode, readonly_mode,
                  cwd, _asks, _wt_fp0):
    """Handle a FINAL step: builder-not-finalized nudge, implicit-final doer
    nudge, task-board gate, claim-vs-reality guard, and the
    progress-gated verify→fix loop — then accept (fire stop + emit the answer).
    Returns "continue"/"return"."""
    from . import _plan_first
    _sig = yield from _plan_first.on_text(st, step)
    if _sig == "continue":
        return "continue"
    _sig = yield from _final_nudges(st, step, builder, strict_finish, _asks)
    if _sig == "continue":
        return "continue"
    _sig = yield from run_guards(st, step, [
        FileEditClaimGuard(cwd, readonly_mode, builder, _wt_fp0),
        ZeroEditGuard(cwd, readonly_mode, builder, plan_mode, _asks, _wt_fp0,
                      strict=strict_finish),
        # A turn that DID change files: the answer against the disk (a file
        # named as changed that no write touched) and against the turn's own
        # commands (a check that failed after the last change).
        UnchangedFileClaimGuard(cwd, readonly_mode, builder, plan_mode),
        UnpassedCheckGuard(readonly_mode, builder, plan_mode)])
    if _sig == "continue":
        return "continue"
    _sig = yield from _verify_on_final(st, step, cwd, plan_mode, builder)
    if _sig == "continue":
        return "continue"
    # FINAL accepted on a multi-part turn: close out the tracker so
    # the dock never ends with stale pending items the model forgot
    # to flip.
    if _asks:
        for _i in range(len(_asks)):
            _slug = f"part-{_i + 1}"
            if st.board.get(_slug, {}).get("status") in ("failed", "skipped"):
                continue            # the model said so; don't overwrite it
            yield {"type": "subtask_update", "slug": _slug, "status": "done"}
    _fire_stop("final", cwd)
    if plan_mode:
        try:
            from .._pause import save as _save_pause
            _save_pause(getattr(st, "session_id", None), st.convo,
                        asked=bool(getattr(st, "plan_asked", False)))
        except Exception:  # noqa: BLE001
            pass
    # done goes out before any next-step prediction; a one-slot server skips it.
    _sugg = None if _endpoint_one_slot() else _start_suggestion(
        _last_user_message(st), _turn_summary(st), cwd)
    # Still-running handed-off jobs outlive this answer as background jobs
    # (end_turn would otherwise kill what the answer calls "still running").
    from aiforge_core.runtime import cmd_jobs_promote as _promote
    _promoted = _promote.promote_turn_jobs()
    _bg_note = _promote.answer_suffix(_promoted)
    # One factual line about what this turn leaves behind (commands still
    # running, uncommitted files, temp paths): from the cleanup inventory,
    # never from the model. Empty when nothing is left.
    try:
        from aiforge_core.runtime import action_log as _alog
        _bg_note += _alog.final_suffix(
            getattr(st, "session_id", None), cwd,
            skip_jobs=[getattr(j, "key", "") for j in _promoted[:4]])
    except Exception:  # noqa: BLE001 — the line never blocks an answer
        pass
    # What the harness measured in this turn (files, commits, commands), so
    # "done" / "already done" / "nothing changed" can be read against it.
    if not (strict_finish or builder or plan_mode):
        from aiforge_core.runtime import turn_facts_line as _facts
        # The block is the harness's: one the model copied from an earlier
        # answer is taken out before the measured one goes in.
        step["text"] = _facts.strip_copied(step.get("text") or "")
        _bg_note = _facts.suffix(getattr(st, "session_id", None), cwd,
                                 getattr(st, "head0", None), _wt_fp0 or "") + _bg_note
    try:
        yield {"type": "message",
               "text": _strip_reasoning_prefix(step["text"]) + _bg_note}
        yield {"type": "done"}
        # The run stays open after done until the producer finishes, and the
        # UI applies a suggestion that arrives then. Wait out the rest of the
        # grace here: the answer is already on screen.
        yield from _collect_suggestion(_sugg, getattr(st, "session_id", None))
    finally:
        _cancel_suggestion(_sugg)
    return "return"


#: An answer that defers the work to a command still running.
_WAITING_RE = re.compile(
    r"\b(?:I(?:'ll| will) )?(?:report back|let you know|keep you posted|get back to you)"
    r"|\bI(?:'ll| will) (?:report|update you|follow up|poll|continue|proceed|run)\b[^.]{0,60}"
    r"\b(?:once|when|after|as soon as)\b"
    r"|\b(?:as soon as|once|when) (?:it|this|that|the \w+) (?:finishes|completes|is done|is ready)"
    r"[^.]{0,40}\bI(?:'ll| will)\b"
    r"|\bI(?:'m| am) (?:polling|waiting for)", re.I)
#: How often one turn is sent back to wait instead of answering.
_BG_WAIT_NUDGES = 3


def _wait_for_own_jobs(st, step):
    """An answer that defers the work to a command of this chat that is
    still running. Returns "continue" (sent back to wait), else None."""
    # "I will report when it finishes" while the job is still running: once
    # the turn ends nothing wakes the agent when it finishes (its result is
    # only posted to the chat), so the work stops there. Make it wait.
    text = str(step.get("text") or "")
    if (getattr(st, "bg_wait_nudges", 0) < _BG_WAIT_NUDGES
            and not getattr(st, "plan_mode", False) and not getattr(st, "readonly_mode", False)
            # The running-job nudge already told it it may answer now.
            and not getattr(st, "running_job_nudged", False)
            and _WAITING_RE.search(text)):
        try:
            from aiforge_core.runtime import cmd_jobs
            turn = cmd_jobs._TURN.get()
            # Background commands only (never a `serve` service, which is
            # meant to keep running): one this turn started, or one the
            # answer names.
            live = [j for j in cmd_jobs.running()
                    if str(j.key).startswith("bg-")
                    and (getattr(j, "turn", None) is turn and turn is not None
                         or re.search(rf"\b{re.escape(str(j.key))}\b", text))]
        except Exception:  # noqa: BLE001 — never block an answer on this
            live = []
        if live:
            st.bg_wait_nudges = getattr(st, "bg_wait_nudges", 0) + 1
            ids = ", ".join(str(j.key) for j in live[:4])
            first = str(live[0].key)
            if step.get("text"):
                yield {"type": "thought", "text": step["text"]}
            yield {"type": "thought", "role": "system",
                   "text": f"⏳ waiting for {ids} instead of ending the turn"}
            st.convo.append({"role": "user", "content":
                f"[harness — not the user] Your answer says you will carry on "
                f"after {ids} finishes, but if you end the turn now nothing "
                f"wakes you when it does — the work stops here. Wait for it "
                f'now: command_wait {{"id": "{first}", "max_s": 600}} (call it '
                f"again if it is still running), then continue the task. End "
                f"the turn only when the task is done or you need the user."})
            return "continue"
    return None


#: An answer that only announces work still to do ("I need to fix X, rebuild
#: it and run the tests") ...
_WORK = (r"(?:fix|rebuild|build|run|rerun|re-run|test|check|update|apply|implement|add|"
         r"write|patch|edit|deploy|commit|push|verify|investigate|debug|create|change|"
         r"refactor|start|restart|retry|try|install|set up|setup|migrate|clean|remove|"
         r"delete|move|rename|wire|hook|finish|complete|continue|proceed|work)")
_INTENT_RE = re.compile(
    r"^\W{0,3}(?:I(?:'m| am) (?:working through|working on|going to|about to|now going)|"
    r"I (?:need|still need|have) to\b|"
    r"(?:I(?:'ll| will)|Next,? I(?:'ll| will)?|Now,? I(?:'ll| will)|Let me) (?:now |next |then |first )?"
    + _WORK + r"\b)", re.I)
#: ... and says nothing was finished.
_DONE_RE = re.compile(
    r"\b(?:done|completed|finished|fixed|passed|passing|succeeded|works now|"
    r"all \d+ tests|here(?:'s| is| are) (?:the|a|what|how)|the (?:cause|problem|issue) "
    r"(?:is|was)|summary|blocked|cannot|can't|need (?:you|your)|"
    r"which (?:one|option)|should I|do you want)\b|\?", re.I)
_INTENT_NUDGES = 2
#: The LAST sentence says it is about to act ("Let me fix it.", "I'll fix that
#: now."): the answer explains, then stops where the work should start.
_TRAILING_INTENT_RE = re.compile(
    r"^(?:so |now |ok(?:ay)?,? )?(?:"
    r"(?:let me|i(?:'ll| will)|i(?:'m| am) going to|next,? i(?:'ll| will)?|now i(?:'ll| will)) "
    r"(?!know\b|keep\b|leave\b|wait\b|stop\b|let you\b|explain\b|describe\b|"
    r"summari[sz]e\b|clarify\b|note\b|answer\b|use\b|be\b|recap\b|remind\b|mention\b|look forward\b)\w+"
    r")\b[^!?]*?[.!]?$", re.I)
#: "Both fixes will happen." — the sentence ends there.
_WILL_HAPPEN_RE = re.compile(
    r"^(?:both |all |the |these |those )?(?:\w+ ){0,3}(?:fix(?:es)?|changes?|steps?|parts?) "
    r"will (?:happen|be done|follow)\s*[.!]?$", re.I)
#: ... unless it is an offer or waits on someone ("I'll push it if you want").
_CONDITIONAL_RE = re.compile(
    r"\b(?:if|once|when|after|unless|later|tomorrow|approve|approval|confirm|you)\b", re.I)


def _last_sentence(text: str) -> str:
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text.strip()) if p.strip()]
    return parts[-1] if parts else ""


#: How often one turn retries after a reply that came without tools.
_TOOLLESS_RETRIES = 3


def _not_from_a_toolless_reply(st, step, builder=None):
    """The tool-enabled call was refused and this reply came from a plain-
    text fallback: the model had no tools, so whatever it wrote ("Let me do
    that now.") is not the answer. Retry the step with tools instead of ending
    the turn (bounded). Not in plan / read-only mode."""
    fn = getattr(st, "complete_fn", None)
    try:
        was = bool(fn is not None and getattr(fn, "last_degraded", None) and fn.last_degraded())
    except Exception:  # noqa: BLE001
        was = False
    # A reply that says FINAL: is an answer even from the fallback; only prose
    # without the marker (``implicit``) is the model talking without tools.
    if (not was or not step.get("implicit") or builder or getattr(st, "plan_mode", False)
            or getattr(st, "readonly_mode", False)
            or getattr(st, "toolless_retries", 0) >= _TOOLLESS_RETRIES):
        return None
    st.toolless_retries = getattr(st, "toolless_retries", 0) + 1
    yield {"type": "thought", "role": "system",
           "text": "⟳ the model's reply came without tool access (the call was "
                   "refused) — trying the step again with tools"}
    st.convo.append({"role": "user", "content":
        "[harness — not the user] Your last reply was made without tool access "
        "(the request with tools failed), so it did not do anything. Continue "
        "the task now with a tool call. Answer only when the work is done or "
        "you need the user."})
    return "continue"


def _do_what_you_said(st, step, builder=None, strict_finish=False):
    """The answer only says what is still to be done — the turn would end
    with the work announced and not done. Sends the model back to do it
    (bounded). Not in plan / read-only mode, and not for an answer that
    reports a result, a cause, a blocker or a question."""
    text = str(step.get("text") or "").strip()
    if (not text or len(text) > 900 or builder or strict_finish
            or getattr(st, "plan_mode", False) or getattr(st, "readonly_mode", False)
            # The running-job nudge already said it may answer while jobs run.
            or getattr(st, "running_job_nudged", False)
            or getattr(st, "intent_nudges", 0) >= _INTENT_NUDGES
            or "?" in text):
        return None
    announced = _INTENT_RE.search(text) and not _DONE_RE.search(text)
    # "The build script has a bug — it copies to /tmp/x without creating it.
    # Let me fix it." explains the cause, then ends where the fix should start.
    last = _last_sentence(text)
    if _CONDITIONAL_RE.search(last):
        return None                  # an offer, or waiting on someone
    about_to_act = (bool(_TRAILING_INTENT_RE.match(last) or _WILL_HAPPEN_RE.match(last))
                    and not _DONE_RE.search(last))
    if not (announced or about_to_act):
        return None
    st.intent_nudges = getattr(st, "intent_nudges", 0) + 1
    yield {"type": "thought", "text": text}
    yield {"type": "thought", "role": "system",
           "text": "▶ the answer only said what is left to do — continuing"}
    st.convo.append({"role": "user", "content":
        "[harness — not the user] Your answer only describes work you still "
        "have to do; ending the turn now leaves it undone. Do it now: take the "
        "next action. End the turn when the work is done (say what you did "
        "and what the result was), or when you are blocked and need the user "
        "(say exactly what you need)."})
    return "continue"


def _handle_continue_step(st, step, builder, cwd):
    """Handle a continue step (narrated-no-action, or empty_final signalled
    completion with no answer): nudge appropriately (bounded), else stop cleanly.
    Returns continue/return."""
    # Two shapes land here. (a) The model narrated a next step
    # (THOUGHT) but emitted no ACTION — truncated turn or dropped
    # protocol line. (b) reason="empty_final": it SIGNALLED completion
    # and wrote no answer, which used to publish the marker itself as
    # the reply ("ACTION: FINAL" in the chat). Both are nudged; the
    # wording differs because the missing thing differs.
    _empty_final = step.get("reason") == "empty_final"
    if step.get("thought"):
        yield {"type": "thought", "text": step["thought"]}
    signal = detect("narration", st)
    if signal and not pause_on_stuck():
        # It keeps not delivering: change approach and carry on (the guard
        # ends the turn with a summary only after many tries).
        _r = yield from change_approach(
            st, "You keep saying what you will do without doing it.")
        if _r == "continue":
            st.continue_nudges = 0
        return _r
    if signal:
        # It keeps not delivering — stop cleanly rather than loop to
        # the safety cap.
        _fire_stop("no_action", cwd)
        if _empty_final:
            # Deliberately NOT "I finished the work": zero tools may
            # have run, and text_doer / analysis_pipeline treat a
            # message that does NOT start with "(stopped:" as a clean
            # outcome — so claiming completion here would poison the
            # pipeline's own quality record. Deliberately no
            # _progress_recap either: that is model-facing text, and it
            # tallies the FINAL markers themselves.
            yield {"type": "message", "text":
                   "(stopped: I signalled I was done but never wrote "
                   "the reply. Ask me to summarise what happened and "
                   "I'll write it up.)"}
        else:
            yield {"type": "message",
                   "text": (step.get("thought") or "").strip()
                   or "I described a next step but couldn't complete the "
                      "action. Could you rephrase or narrow the request?"}
        yield {"type": "done"}
        return "return"
    if _empty_final and builder:
        # A builder session's "answer" is an ARTIFACT: it must call its
        # finalize tool. Telling it "reply with FINAL, do not emit
        # ACTION" is the exact opposite instruction, and the turn would
        # end claiming success with nothing created.
        _fin = _BUILDER_FINALIZE_TOOL.get(builder, _THE_FINALIZE_TOOL)
        st.convo.append({"role": "user", "content":
                      f"You signalled you were finished but never called "
                      f"`{_fin}`, so nothing was created. Call `{_fin}` NOW "
                      f"with the values you have collected."})
    elif step.get("reason") == "broken_call":
        _tool = step.get("tool") or "the tool"
        st.convo.append({"role": "user", "content":
                      f"[harness — not the user] Your last reply wrote a "
                      f"`{_tool}` call as text, and it was cut off or is not "
                      f"valid JSON, so it did NOT run. Make the call with the "
                      f"tool itself, not as text. For a large edit, use "
                      f"several smaller calls (a few lines of old_text / "
                      f"new_text each), or file_write for a whole new file."})
    elif _empty_final:
        # The work is done; what is missing is the reply. "Emit an
        # ACTION" is the wrong instruction for that.
        st.convo.append({"role": "user", "content":
                      "You signalled you were finished but wrote no "
                      "answer — the user saw nothing. Reply now with "
                      "`FINAL: <answer>` where <answer> tells them what "
                      "you did and what it means for their request, in "
                      "plain prose. Do not emit ACTION, THOUGHT or any "
                      "other marker."})
    else:
        st.convo.append({"role": "user",
                      "content": "You described your next step but did NOT "
                      "emit an ACTION. Continue now — output the next ACTION "
                      "(tool call) to make progress, or `FINAL: <answer>` if "
                      "you are genuinely done. Do not just narrate."})
    # NOT `n += 1`: the loop head already counted this iteration, and
    # the sibling implicit-final nudge does not double-charge either.
    # On a 6-step Quick turn the double charge turned one nudge into a
    # "used up Quick mode's step budget" stop.
    return "continue"
