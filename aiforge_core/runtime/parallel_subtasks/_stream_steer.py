"""Steering a running team: pinning a message to a subtask, spec mandates, and
draining queued steers."""
from __future__ import annotations


def _pkg():
    """``_stream``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``_stream``; patch any other
    name on this module."""
    import aiforge_core.runtime.parallel_subtasks._stream as package
    return package


def _pin_to_subtask(subs: list, target: str, text: str,
                    note: str) -> tuple[str, str] | None:
    """Attach the mandate to ONE subtask. None when that subtask is gone (the
    caller then treats it as a global steer)."""
    hit = next((s for s in subs if s.get("slug") == target), None)
    if hit is None:
        return None
    mandate = f"\n[MANDATORY user instruction — MUST satisfy]: {text}"
    hit["goal"] = (hit.get("goal") or "") + mandate
    hit["_user_mandate"] = (hit.get("_user_mandate") or []) + [text]
    label = hit.get("path") or target
    return (f"## ⚙ User instruction (MANDATORY) → {label}",
            f"✅ Got it — treating as a **must** for **{label}**"
            + (f" — {note}" if note else "")
            + ". Pinned to that subtask + SPEC; it rebuilds until satisfied.")


def _steer_headings(target: str, note: str) -> tuple[str, str]:
    """``(SPEC heading, user-facing confirmation)`` for a non-subtask steer."""
    if target == "new":
        return ("## ⚙ User instruction (MANDATORY — NEW requirement)",
                "✅ Got it — new **must-have** requirement"
                + (f" — {note}" if note else "")
                + ". Pinned to SPEC; the reconcile pass builds + verifies it.")
    return ("## ⚙ User instruction (MANDATORY — whole build)",
            "✅ Got it — treating as a **must** across the whole build"
            + (f" — {note}" if note else "")
            + ". Pinned to SPEC; every remaining subtask + the reconcile must "
              "satisfy it.")


def _append_spec_mandate(cwd: str, heading: str, text: str) -> str:
    """Write the mandate into SPEC.md. Returns "" on success, else the reason.

    This used to swallow the write error, so a steer the run could not record
    still answered "folded into the plan" — the user believed their new
    requirement was in the spec when the file had never been touched.
    """
    err = ""
    try:
        from aiforge_core.runtime.team_workspace import spec_path
        with open(spec_path(cwd), "a", encoding="utf-8") as fh:
            fh.write(f"\n\n{heading}\n- **MUST:** {text}\n")
    except Exception as exc:  # noqa: BLE001
        err = str(exc)
    # Record globally either way: the reconcile prompt re-asserts these, so a
    # failed spec write still leaves the requirement binding on the merge.
    try:
        _USER_MANDATES.setdefault(cwd, []).append(text)
    except Exception:  # noqa: BLE001
        pass
    return err


def _apply_steer(text: str, subs: list, cwd: str) -> str:
    """Route ONE steer to its target and pin it. Returns the confirmation."""
    try:
        route = _pkg()._route_steering(text, subs)
        target, note = route["target"], route["note"]
    except Exception as exc:  # noqa: BLE001
        # Routing asks the model which subtask this belongs to. If it is down,
        # the steer is still a REQUIREMENT — treat it as global rather than
        # dropping the user's instruction on the floor.
        log.warning("steer routing failed (%s) — treating as global", exc)
        target, note = "global", "could not classify this steer, applied to the whole run"
    # A user comment is a MANDATORY requirement, not a hint — the subtask build
    # and the final reconcile MUST satisfy it. A steer naming a subtask that no
    # longer exists falls back to a global one.
    pinned = (_pin_to_subtask(subs, target, text, note)
              if target not in ("global", "new") else None)
    heading, feedback = pinned or _steer_headings(
        "new" if target == "new" else "global", note)
    err = _append_spec_mandate(cwd, heading, text)
    if err:
        return (f"{feedback}\n\n⚠ could NOT write it into SPEC.md ({err}). "
                "It is still binding on the final reconcile, but the spec "
                "document does not show it — fix the workspace and re-state it "
                "if the subtasks need to read it.")
    return feedback


def _cancel_checker_for(session_id):
    def _cancelled() -> bool:
        if session_id is None:
            return False
        try:
            from aiforge_core.runtime import chat_cancel
            return chat_cancel.is_cancelled(session_id)
        except Exception:  # noqa: BLE001
            return False
    return _cancelled


def _steering_drain(session_id, subs: list, cwd: str):
    """Fold any mid-run steering comment into the run — but first ANALYSE it:
    which subtask/topic it targets (or a global change, or an entirely NEW
    requirement) — tell the user how it was read, then route it (annotate that
    subtask's goal + the right SPEC.md section) so the remaining subtasks +
    reconcile pick it up. Yields feedback events."""
    if session_id is None:
        return
    try:
        from aiforge_core.runtime import chat_interject, chat_steer
        if not chat_interject.pending(session_id):
            return
        # drain() REMOVES the pending steers, so they exist only here. A raise
        # partway through used to abandon the rest of the list — the user's
        # second and third instructions were gone with no message at all.
        drained = list(chat_interject.drain(session_id))
    except Exception as exc:  # noqa: BLE001
        log.warning("steering drain failed: %s", exc)
        return
    for raw in drained:
        text = (raw or "").strip()
        if not text:
            continue
        # Echo the user's steer TEXT (role:steer) so it shows + persists in
        # the UI for team mode too — same as the simple/plan loop.
        try:
            yield chat_steer.steer_event(text)
            yield {"type": "thought", "role": "planner",
                   "text": _pkg()._apply_steer(text, subs, cwd)}
        except Exception as exc:  # noqa: BLE001
            # Never silent: an instruction the run could not apply has to be
            # visible, or the user waits for a change that will never come.
            log.warning("could not apply steer %r: %s", text[:80], exc)
            yield {"type": "thought", "role": "planner",
                   "text": f"⚠ could not apply your instruction ({exc}). "
                           f"It was NOT added to the plan: {text[:200]}"}


def _arm_session(session_id) -> None:
    """Accept mid-run steering for this run, and bind its subprocesses
    (integration build/pytest) to the session so Stop kills them."""
    if session_id is None:
        return
    for module, fn, args in (("chat_interject", "set_steerable",
                              (session_id, True)),
                             ("chat_cancel", "set_active", (session_id,))):
        try:
            mod = __import__(f"aiforge_core.runtime.{module}", fromlist=[fn])
            getattr(mod, fn)(*args)
        except Exception:  # noqa: BLE001
            pass


# Mid-run user instructions per cwd — MANDATORY constraints re-asserted into the
# reconcile prompt so a user's "must" survives every rebuild/fix pass.
_USER_MANDATES: dict[str, list[str]] = {}


# ---- cross-group names (bottom import = cycle-safe; all defs above are set) ----
from ._worktree import log
