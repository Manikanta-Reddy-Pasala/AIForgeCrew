"""Per-ticket workspace / environment plumbing.

Resolves (and later restores) the ``AIFORGE_REPO_ROOT`` / ``AIFORGE_AFM_REPO``
env vars for a claimed ticket, copies operator-uploaded attachments into the
per-ticket worktree, and stashes image attachments into AiForgeMemory. Split
out of the orchestrator so the ticket loop stays readable.
"""
from __future__ import annotations

import os

from ._base import log


def _setup_ticket_workspace(ticket) -> tuple[str | None, dict]:
    """Resolve per-ticket worktree and pin ``AIFORGE_REPO_ROOT`` to it.

    The Doer's sandboxed file tools (:mod:`sandbox.root`) and
    :mod:`git_pr` both read ``AIFORGE_REPO_ROOT`` to choose the working
    directory. Without this hook every ticket lands on whatever the
    operator's systemd EnvironmentFile pinned — usually a stale repo.

    Returns ``(worktree_path, prior_env)``. Caller MUST pass
    ``prior_env`` back to :func:`_restore_env` in a finally block.
    """
    from aiforge_core.runtime.workspace import ensure_branch_and_worktree

    # Capture BOTH env vars we override per-ticket so the finally can
    # restore them. AIFORGE_AFM_REPO scopes memory recall (unified_query
    # afm_bundle/xrepo, memory_lookup, impacted_tests) to THIS ticket's
    # repo — without it those sources fall back to a process-global that
    # is never set (→ dead) or, if exported once, leaks another repo's
    # context into every ticket (the ONE-2 class bug).
    prior = {
        "AIFORGE_REPO_ROOT": os.environ.get("AIFORGE_REPO_ROOT"),
        "AIFORGE_AFM_REPO": os.environ.get("AIFORGE_AFM_REPO"),
        "AIFORGE_CURRENT_TICKET": os.environ.get("AIFORGE_CURRENT_TICKET"),
    }
    project = (getattr(ticket, "project", "") or "").strip()
    if project:
        os.environ["AIFORGE_AFM_REPO"] = project
    # Expose the current ticket so doer tools (e.g. subtask_update) can flip
    # this ticket's internal subtask status as the agent works.
    os.environ["AIFORGE_CURRENT_TICKET"] = getattr(ticket, "identifier", "") or ""
    worktree = ensure_branch_and_worktree(ticket)
    if worktree:
        os.environ["AIFORGE_REPO_ROOT"] = worktree
        log.info("ticket=%s workspace=%s afm_repo=%s",
                 ticket.identifier, worktree, project or "-")
    else:
        log.warning(
            "ticket=%s no worktree (project=%r) — falling back to env",
            ticket.identifier, ticket.project,
        )
    return worktree, prior


def _restore_env(prior) -> None:
    # ``prior`` is the dict captured by _setup_ticket_workspace. Tolerate a
    # bare string for back-compat (older call shape = REPO_ROOT only).
    if isinstance(prior, str) or prior is None:
        prior = {"AIFORGE_REPO_ROOT": prior, "AIFORGE_AFM_REPO":
                 os.environ.get("AIFORGE_AFM_REPO")}
    for key, val in prior.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val


def _resolve_attachment_src(f: dict, fallback_base: str) -> "str | None":
    """The on-disk source path for one attachment record: its ``abs_path``, else
    ``fallback_base/<path>`` (tickets created before abs_path existed). None when
    neither resolves to a file."""
    src = f.get("abs_path")
    if src and os.path.isfile(src):
        return src
    candidate = os.path.join(fallback_base, f.get("path") or "")
    return candidate if os.path.isfile(candidate) else None


def _copy_one_attachment(f: dict, dest_dir: str, fallback_base: str,
                         ticket) -> bool:
    """Copy one attachment into ``dest_dir``. Returns True on success; logs and
    returns False when the record is malformed, the source is missing, or the
    copy fails."""
    if not isinstance(f, dict) or not f.get("name"):
        return False
    name = f["name"]
    src = _resolve_attachment_src(f, fallback_base)
    if not src:
        log.warning("ticket=%s attachment missing on disk name=%r",
                    ticket.identifier, name)
        return False
    import shutil
    try:
        shutil.copy2(src, os.path.join(dest_dir, name))
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("ticket=%s attachment copy failed name=%r: %s",
                    ticket.identifier, name, exc)
        return False


def _materialize_attachments_in_worktree(ticket, worktree: str) -> None:
    """Copy operator-uploaded files into the per-ticket worktree.

    The API persists attachments under its own
    ``AIFORGE_REPO_ROOT/.aiforge/ticket-files/{identifier}/``; the runner then
    rebinds ``AIFORGE_REPO_ROOT`` to a per-ticket git worktree. Without this copy
    the Doer prompt's ``.aiforge/ticket-files/{id}/<name>`` relative path
    resolves to a missing file inside the worktree. Skips silently when the
    ticket has no attachments or the worktree is missing.
    """
    if not worktree or not os.path.isdir(worktree):
        return
    files = (ticket.metadata or {}).get("attached_files") or []
    if not isinstance(files, list) or not files:
        return
    dest_dir = os.path.join(worktree, ".aiforge", "ticket-files",
                            ticket.identifier)
    os.makedirs(dest_dir, exist_ok=True)
    fallback_base = os.path.expanduser(os.environ.get(
        "AIFORGE_TICKET_FILES_BASE", "~/codeRepo/Scheduler"))
    copied = sum(_copy_one_attachment(f, dest_dir, fallback_base, ticket)
                 for f in files)
    if copied:
        log.info("ticket=%s materialized %d attachment(s) into worktree",
                 ticket.identifier, copied)


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def _ticket_media_paths(ticket) -> list[str]:
    """The image-attachment paths on a ticket."""
    files = (ticket.metadata or {}).get("attached_files") or []
    paths = [str(f.get("path", "")) for f in files
             if isinstance(f, dict)
             and str(f.get("name", "")).lower().endswith(_IMAGE_EXTS)]
    return [p for p in paths if p]


def _persist_media_sqlite(summary: str, media_paths: list, ticket) -> None:
    """Write the media observation to the embedded SQLite store. Never raises."""
    try:
        from aiforge_core.memory import sqlite_memory as _sqlmem
        _sqlmem.write_unit(
            text=summary, kind="attachment", source="adk_runner",
            tags=[f"ticket:{ticket.identifier}", "kind:vision"],
            repo=ticket.project, ticket=ticket.identifier,
            metadata={"media_refs": media_paths})
    except Exception as exc:  # noqa: BLE001
        log.debug("vision persist[sqlite] failed: %s", exc)


def _persist_ticket_media(ticket) -> None:
    """Gap-10 wire-in: stash image attachments as an AFM ``Observation_v2`` with
    ``media_refs`` populated, so future tickets can recall "we saw screenshot X
    last time" via the same memory_block path the Doer reads. Vision sub #6
    attaches images for the run, but the bytes vanish when the ADK session tears
    down. Soft-fail — never raises into the ticket loop.
    ``AIFORGE_VISION_PERSIST=0`` opts out."""
    if os.environ.get("AIFORGE_VISION_PERSIST", "1") in ("0", "false", ""):
        return
    media_paths = _ticket_media_paths(ticket)
    if not media_paths or not ticket.project:
        return
    summary = (f"Ticket {ticket.identifier} included {len(media_paths)} image "
               f"attachment(s): "
               + ", ".join(p.rsplit("/", 1)[-1] for p in media_paths))
    _persist_media_sqlite(summary, media_paths, ticket)
