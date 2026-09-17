"""The prompt a team pipeline run starts from: the user's request with its
history, resume brief, spec and context blocks."""
from __future__ import annotations


def _history_preamble(history: list[dict] | None) -> str:
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
    return "CONVERSATION SO FAR (continue with this context):\n" + "\n".join(lines)


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
    convo = _history_preamble(history)
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
    return prompt, team_state
