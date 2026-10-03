"""Which project a chat belongs to — the one key recall is scoped by.

A chat's memory is filed and recalled under the project its working folder is
in. What a PAST chat said (its outcome, the files it touched) belongs to that
chat's project too, so recall into another chat is decided here, from the
session's stored ``cwd`` — never from the words in the message.
"""
from __future__ import annotations

#: Shown with anything recalled from a different project than the chat's own.
OTHER_PROJECT_NOTE = ("from another project — its files, paths and results "
                      "do not apply to this one")


def project_key(cwd: "str | None") -> str:
    """Repo key for a chat working in ``cwd`` — the GIT-TOPLEVEL basename (so a
    subdir resolves the same repo as the root), falling back to the raw cwd
    basename, then ``AIFORGE_AFM_REPO``, then the literal ``"repo"``."""
    from aiforge_core.runtime import repo_ident as _ri
    # A chat that was not opened on a project runs in its own scratch folder.
    # Keying memory by that folder ("session-12") filed every such chat's
    # learnings under a scope no later chat reads; they share one bucket.
    if _ri.is_chat_scratch(cwd):
        from aiforge_core.memory.projects import GENERAL
        return GENERAL
    return _ri.repo_name(cwd, sentinel="repo")


def session_project(cwd: "str | None") -> str:
    """The project key of a STORED session. A session with no folder recorded
    is not pinned to any project: it is in the general bucket."""
    if not (cwd or "").strip():
        from aiforge_core.memory.projects import GENERAL
        return GENERAL
    return project_key(cwd)


def other_project_label(project: str) -> str:
    """How a recalled item from ``project`` is marked in a prompt or a tool
    result, so its paths are not taken for this project's."""
    return f"project {project}: {OTHER_PROJECT_NOTE}"


__all__ = ["OTHER_PROJECT_NOTE", "project_key", "session_project",
           "other_project_label"]
