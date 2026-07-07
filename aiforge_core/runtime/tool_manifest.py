"""Single declaration of the tools that MUST exist on BOTH agent surfaces —
the chat ReAct registry (``chat_agent.TOOLS``, ``handler(args, cwd) -> dict``)
and the pipeline Doer (``doer_tools.__all__``, typed ``FunctionTool``).

The two surfaces keep separate implementations (different calling conventions),
which is why a tool added to one and forgotten on the other silently worked in
chat and not in the pipeline (or vice-versa). This list is the contract; both
the CI parity test AND a startup check validate against it, so drift fails
loudly — on the box, not only in CI.

Aliased primitives (grep/grep_repo, editor/multi_edit) are intentionally NOT
here — they differ by name, not capability. Chat-only (set_repo_folder, serve…)
and Doer-only (think, finish, subtask_update…) tools are likewise excluded.
"""
from __future__ import annotations

CROSS_SURFACE: "frozenset[str]" = frozenset({
    # issue trackers / wiki / vcs
    "jira_search", "jira_create", "jira_update", "jira_comment",
    "confluence_search", "confluence_create", "confluence_update",
    "gitlab_search", "gitlab_create", "email_send",
    # code verification
    "typecheck", "run_tests", "format", "lsp", "multi_edit",
    # file ops
    "file_read", "file_write", "file_patch",
    # self-improvement
    "learn_skill", "learn_workflow",
})


def missing() -> dict:
    """Return which cross-surface tools are absent from each registry:
    ``{"chat": [...], "doer": [...]}`` — both empty means the surfaces agree."""
    chat: set = set()
    doer: set = set()
    try:
        from aiforge_core.runtime import chat_agent as _ca
        chat = set(getattr(_ca, "TOOLS", {}))
    except Exception:  # noqa: BLE001
        pass
    try:
        from aiforge_core.runtime import doer_tools as _dt
        doer = set(getattr(_dt, "__all__", ()))
    except Exception:  # noqa: BLE001
        pass
    return {"chat": sorted(t for t in CROSS_SURFACE if t not in chat),
            "doer": sorted(t for t in CROSS_SURFACE if t not in doer)}


def validate_or_warn() -> dict:
    """Log a WARNING for any cross-surface drift (startup check). Never raises."""
    import logging
    m = missing()
    if m["chat"] or m["doer"]:
        logging.getLogger("aiforge.tools").warning(
            "TOOL DRIFT — cross-surface tools missing: chat=%s doer=%s "
            "(add to both registries + tool_manifest)", m["chat"], m["doer"])
    return m


__all__ = ["CROSS_SURFACE", "missing", "validate_or_warn"]
