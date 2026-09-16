"""Finishing a turn: verify-on-final, false-claim guard, final nudges, the
turn summary, next-step suggestion, and the stuck-output guard."""
from __future__ import annotations

import subprocess

from .._context import (
    _OUTPUT_REPEAT,
    _claims_file_edits,
    _edit_claim_disclaimer,
    _edit_claim_guard_enabled,
    _edit_claim_nudge,
    _fire_stop,
    _progress_recap,
    _repo_name,
    _run_project_verify,
    _stuck_recovery_max,
    _text_of,
    _verify_fix_message,
    _verify_max_rounds,
    _verify_on_final_enabled,
    _worktree_fingerprint,
)
from .._prompt import _strip_reasoning_prefix
from .._registry import (
    _BUILDER_FINALIZE_TOOL,
)
from ._shared import (
    _THE_FINALIZE_TOOL,
    _log,
)


def _verify_on_final(st, cwd, plan_mode, builder):
    """Progress-gated verify->fix on FINAL: only an act-mode run that edited
    files with a real suite; loop while failures drop, else accept honestly.
    Returns continue or None."""
    # A + B: enforced verify→fix on FINAL (progress-gated). Only for an
    # act-mode run that actually EDITED files with a real test suite —
    # a Q&A turn (0 edits) or read-only plan mode is untouched. Keep
    # looping while the failure count DROPS; once it stalls (2 rounds no
    # improvement) accept the HONEST still-failing final rather than
    # churn. This gives simple/doer runs the pipeline's no-false-green
    # guarantee. Opt out: AIFORGE_CHAT_VERIFY_ON_FINAL=0.
    if (not plan_mode and not builder and st.edits_made > 0
            and st.verify_rounds < _verify_max_rounds()
            and _verify_on_final_enabled()):
        _vok, _vout = _run_project_verify(cwd)
        if _vok is False:
            try:
                from aiforge_core.runtime.parallel_subtasks import _fail_count
                _fails = _fail_count(_vout)
            except Exception:  # noqa: BLE001
                _fails = 1
            if st.verify_prev_fails is not None and _fails >= st.verify_prev_fails:
                st.verify_stalls += 1
            else:
                st.verify_stalls = 0
            st.verify_prev_fails = _fails
            if st.verify_stalls < 2:
                st.verify_rounds += 1
                yield {"type": "thought", "role": "system",
                       "text": f"✗ tests failing ({_fails}) — fixing "
                               f"(verify round {st.verify_rounds}/"
                               f"{_verify_max_rounds()})…"}
                st.convo.append({"role": "user",
                              "content": _verify_fix_message(_vout)})
                return "continue"
            yield {"type": "thought", "role": "system",
                   "text": f"⚠ tests still failing ({_fails}) after "
                           f"{st.verify_rounds} fix rounds — stopping with "
                           "the honest state."}
    return None


def _claim_guard(st, step, cwd, readonly_mode, builder, _wt_fp0):
    """Claim-vs-reality guard: when the model claims edits but landed zero and
    the tree is unchanged, nudge it to write (bounded); on the last try prepend
    an honest disclaimer. Returns continue or None."""
    # Claim-vs-reality guard: the model asserts it edited/created files
    # but landed ZERO edits this turn AND the working tree is unchanged
    # (checked against every tool + any on-disk write, not just counted
    # ones) — a hallucinated tool-use surfaced as prose (the frequent
    # "I applied the fix to X / Confirmed Fixes Applied" with no diff).
    # Nudge it to actually write (bounded); if it still won't, prepend an
    # honest note so the user is never told a change landed that didn't.
    # Opt out: AIFORGE_CHAT_EDIT_CLAIM_GUARD=0.
    # Disk cross-check: "" = no git signal (honor the contract — NOT
    # "clean"), so in a non-git workspace we rely on _edits_made==0 alone;
    # with git, fire only when the tree is UNCHANGED (a real write would
    # have dirtied it — an incidental dirty tree suppressing the guard is
    # an accepted conservative miss).
    _wt_now = (_worktree_fingerprint(cwd)
               if _edit_claim_guard_enabled() else "")
    _no_landed_write = (_wt_now == "" or _wt_now == _wt_fp0)
    if (not readonly_mode and not builder and st.edits_made == 0
            and _edit_claim_guard_enabled()
            and _claims_file_edits(step.get("text") or "")
            and _no_landed_write):
        if st.edit_claim_nudges < 2:
            st.edit_claim_nudges += 1
            if step.get("text"):
                yield {"type": "thought", "text": step["text"]}
            yield {"type": "thought", "role": "system",
                   "text": "⚠ you described file edits but no write ran "
                           "and nothing changed on disk — applying for "
                           "real…"}
            st.convo.append({"role": "user", "content": _edit_claim_nudge()})
            return "continue"
        step["text"] = _edit_claim_disclaimer(step.get("text") or "")
    return None


def _final_nudges(st, step, builder, strict_finish, _asks):
    """Pre-accept FINAL nudges: builder-not-finalized reminder, implicit-final
    doer nudge (strict_finish), and the one-time multi-ask completeness gate.
    Returns continue to loop again, or None to proceed."""
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
    # Multi-ask completeness gate (once): before accepting FINAL on a
    # multi-part message, make the model self-check its answer against
    # the checklist — the #1 simple-mode complaint is answering ask 1
    # and silently dropping the rest.
    if _asks and not st.multiask_checked and not builder:
        st.multiask_checked = True
        yield {"type": "thought", "role": "system",
               "text": f"✔ checking all {len(_asks)} parts of the "
                       "request are addressed…"}
        st.convo.append({"role": "user", "content":
            "[completeness check — not the user] The user's message "
            f"contained {len(_asks)} distinct asks:\n"
            + "\n".join(f"{i + 1}. {a}" for i, a in enumerate(_asks))
            + "\nRe-read your answer above. If EVERY ask is addressed, "
            "resend it unchanged as FINAL. If any is missing, do the "
            "missing work now (ACTIONs as needed) and produce ONE "
            "complete FINAL covering all parts, numbered."})
        return "continue"
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
        out = subprocess.run(["git", "status", "--porcelain"], cwd=str(cwd),
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
        names = [str(k) for k, v in counts.items() if v]
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
    shells out to git on every turn end, so building the argument first meant a
    disabled feature still paid for a subprocess per turn — "off" has to mean
    it costs nothing, not merely that it emits nothing.
    """
    from aiforge_core.runtime import next_step
    from aiforge_core.runtime.next_step import _predict as _np

    if _np._disabled():
        return None
    return next_step.predict({"message": message, "did": did,
                              "repo": _repo_name(str(cwd or "")),
                              "clean_tree": _is_clean_tree(cwd)})


def _emit_suggestion(message: str, did: str, cwd):
    """Yield at most one ``suggestion`` event. Never raises.

    Emitted AFTER the answer and before ``done`` — the same ordering
    ``plan_ready`` uses. The user reads what they asked for either way, so a
    prediction that is slow, wrong or broken costs them nothing.
    """
    try:
        p = _predict_next_step(message, did, cwd)
    except Exception as exc:  # noqa: BLE001 — a prediction never breaks a turn
        _log.debug("next_step: prediction skipped: %s", exc)
        return
    if p is not None:
        yield p.as_event()


def _handle_final(st, step, builder, strict_finish, plan_mode, readonly_mode,
                  cwd, _asks, _wt_fp0):
    """Handle a FINAL step: builder-not-finalized nudge, implicit-final doer
    nudge, multi-ask completeness gate, claim-vs-reality guard, and the
    progress-gated verify→fix loop — then accept (fire stop + emit the answer).
    Returns "continue"/"return"."""
    _sig = yield from _final_nudges(st, step, builder, strict_finish, _asks)
    if _sig == "continue":
        return "continue"
    _sig = yield from _claim_guard(st, step, cwd, readonly_mode, builder, _wt_fp0)
    if _sig == "continue":
        return "continue"
    _sig = yield from _verify_on_final(st, cwd, plan_mode, builder)
    if _sig == "continue":
        return "continue"
    # FINAL accepted on a multi-part turn: close out the tracker so
    # the dock never ends with stale pending items the model forgot
    # to flip.
    if _asks:
        for _i in range(len(_asks)):
            yield {"type": "subtask_update",
                   "slug": f"part-{_i + 1}", "status": "done"}
    _fire_stop("final", cwd)
    yield {"type": "message", "text": _strip_reasoning_prefix(step["text"])}
    yield from _emit_suggestion(_last_user_message(st), _turn_summary(st), cwd)
    yield {"type": "done"}
    return "return"


def _stuck_output_guard(st, out):
    """Stuck-output guard: on N identical model replies, first recover with a
    progress recap + nudge (bounded); if it keeps repeating, stop and ask the
    user. Returns continue/return/None."""
    # Stuck-output loop: identical model reply N times running. A local model
    # deep in a long tool chain (esp. a many-file read sweep) loses track and
    # re-emits an action it already ran — so FIRST recover with a progress
    # recap + "do the NEXT step" nudge (bounded); only give up if that keeps
    # failing. The old hard bail here discarded all the work done so far.
    st.recent_outputs.append(out.strip())
    if (len(st.recent_outputs) == _OUTPUT_REPEAT
            and len(set(st.recent_outputs)) == 1):
        if st.stuck_recoveries < _stuck_recovery_max():
            st.stuck_recoveries += 1
            st.recent_outputs.clear()          # fresh slate for the recovered plan
            _recap = _progress_recap(st.convo)
            yield {"type": "thought", "role": "system",
                   "text": "↺ repeated output — recap + nudge to continue"}
            # Append the repeated assistant turn BEFORE the nudge — else two
            # consecutive user turns (the prior OBSERVATION + this nudge)
            # break providers like claude_local.
            st.convo.append({"role": "assistant", "content": out})
            st.convo.append({"role": "user", "content":
                "[loop guard — not the user] You repeated the SAME output — "
                "that makes no progress. "
                + (_recap + ". " if _recap else "")
                + "Take the NEXT, DIFFERENT step now: act on something not yet "
                "done (e.g. the next unread file), or output `FINAL: <answer>` "
                "if the task is fully complete. Do NOT repeat a previous action."})
            return "continue"
        yield {"type": "message", "awaiting_input": True,
               "text": "I seem to be going in circles on this. Could you "
                       "clarify what you'd like me to do, or give a bit "
                       "more detail? (I stopped rather than keep retrying "
                       "the same thing.)"}
        yield {"type": "done"}
        return "return"

    return None
