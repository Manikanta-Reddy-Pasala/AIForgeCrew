"""The prompt a team pipeline run starts from: the user's request with its
history, resume brief, spec and context blocks."""
from __future__ import annotations


#: Appended to the planner-facing prompt when the request asks for changes.
EXECUTE_DIRECTIVE = (
    "THIS REQUEST ASKS FOR CHANGES. The run is finished only when the files "
    "have been edited and the result checked — a plan, review, research note "
    "or status summary is NOT the deliverable. Plan fresh for THIS request: "
    "anything above that says the work is done, \"N% built\", or lists an "
    "earlier task board as complete is an UNVERIFIED claim from a past turn — "
    "check the files on disk, and treat earlier tasks as history, not as "
    "progress on this request. Then implement it.")


def _history_preamble(history: list[dict] | None,
                      unverified: bool = False) -> str:
    """Render prior turns so the team pipeline has conversation continuity
    (it starts a fresh ADK session per message and would otherwise be
    clueless on follow-ups). Drops the trailing current user message."""
    if not history:
        return ""
    prior = list(history)
    if prior and prior[-1].get("role") == "user":
        prior = prior[:-1]
    if not prior:
        return ""
    lines = []
    for m in prior[-12:]:
        who = "User" if m.get("role") == "user" else "Assistant"
        lines.append(f"{who}: {(m.get('content') or '')[:800]}")
    head = ("CONVERSATION SO FAR (earlier turns are context only — their claims "
            "that work is done are unverified; check the disk):"
            if unverified else "CONVERSATION SO FAR (continue with this context):")
    return head + "\n" + "\n".join(lines)


def _build_team_prompt(cwd, prompt, history, session_id, resume_brief):
    """Build the planner-facing prompt (project summary + prior conversation +
    session images + the current request) and the pipeline STATE keys.

    ONE shared context bundle — same source-selection/scoping/gating as single
    chat (context_bundle.build_bundle), so team-chat can never silently miss a
    source the single path injects. A resume brief is CONTEXT, not the request,
    so it joins here (raw_prompt stays the user's actual ask — what gets
    persisted, memoized, and used as the recall query). Returns
    ``(prompt, team_state)``."""
    raw_prompt = prompt
    cave = False
    _ctx_on = lambda _b: True  # noqa: E731
    try:
        from aiforge_core.runtime.chat_agent import _cave_mode, _ctx_on
        cave = _cave_mode()
    except Exception:  # noqa: BLE001
        pass
    from aiforge_core.runtime import context_bundle as _cb
    bundle = _cb.build_bundle(cwd, raw_prompt, cave=cave, ctx_on=_ctx_on,
                              session_id=session_id, want_repo_map=False)
    from .chat_router import wants_changes
    _wants = wants_changes(raw_prompt)
    convo = _history_preamble(history, unverified=_wants)
    img_ctx = ""
    if session_id is not None:
        try:
            from aiforge_core.runtime import chat_media
            img_ctx = chat_media.context_block(session_id)
        except Exception:  # noqa: BLE001
            img_ctx = ""
    parts = [p for p in (*bundle.blocks(), img_ctx, convo) if p]
    prompt = ("\n\n".join(parts) + f"\n\nCURRENT REQUEST:\n{prompt}"
              if parts else prompt)
    if _wants:
        prompt = f"{prompt}\n\n{EXECUTE_DIRECTIVE}"
    if resume_brief:
        prompt = f"{prompt}\n\n{resume_brief}"
    # ALSO expose these as pipeline STATE keys — many graph nodes run
    # include_contents='none' and read the {rules_md?}/{memory_brief_md?}/
    # {user_prefs_md?} placeholders, NOT the seed prose above.
    team_state = {"chat_cwd": cwd}
    if bundle.rules_md:
        team_state["rules_md"] = bundle.rules_md
    if bundle.memory_md:
        team_state["memory_brief_md"] = bundle.memory_md
    if bundle.preferences_md:
        team_state["user_prefs_md"] = bundle.preferences_md
    try:
        # A resumed team run starts from what the unfinished one learned: the
        # approaches that failed and the Doer handoff (runtime/handoff_store).
        from aiforge_core.runtime import handoff_store
        team_state.update(handoff_store.team_seed(session_id))
    except Exception:  # noqa: BLE001
        pass
    return prompt, team_state
