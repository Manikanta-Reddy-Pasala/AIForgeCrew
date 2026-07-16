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


def _materialize_attachments_in_worktree(ticket, worktree: str) -> None:
    """Copy operator-uploaded files into the per-ticket worktree.

    The API persists attachments under its own
    ``AIFORGE_REPO_ROOT/.aiforge/ticket-files/{identifier}/`` (typically
    a shared workspace). The runner then rebinds
    ``AIFORGE_REPO_ROOT`` to a per-ticket git worktree. Without this copy
    the Doer prompt's ``.aiforge/ticket-files/{id}/<name>`` relative
    path resolves to a missing file inside the worktree.

    Strategy: copy each upload by absolute path (stored as ``abs_path``
    at upload time; falls back to the api's historical default base
    for tickets created before that field existed). Skips silently
    when the ticket has no attachments or the worktree is missing.
    """
    import shutil
    if not worktree or not os.path.isdir(worktree):
        return
    md = ticket.metadata or {}
    files = md.get("attached_files") or []
    if not isinstance(files, list) or not files:
        return
    dest_dir = os.path.join(
        worktree, ".aiforge", "ticket-files", ticket.identifier,
    )
    os.makedirs(dest_dir, exist_ok=True)
    fallback_base = os.path.expanduser(os.environ.get(
        "AIFORGE_TICKET_FILES_BASE", "~/codeRepo/Scheduler",
    ))
    copied = 0
    for f in files:
        if not isinstance(f, dict):
            continue
        name = f.get("name")
        if not name:
            continue
        src = f.get("abs_path")
        if not src or not os.path.isfile(src):
            rel = f.get("path") or ""
            candidate = os.path.join(fallback_base, rel)
            src = candidate if os.path.isfile(candidate) else None
        if not src:
            log.warning(
                "ticket=%s attachment missing on disk name=%r",
                ticket.identifier, name,
            )
            continue
        try:
            shutil.copy2(src, os.path.join(dest_dir, name))
            copied += 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "ticket=%s attachment copy failed name=%r: %s",
                ticket.identifier, name, exc,
            )
    if copied:
        log.info(
            "ticket=%s materialized %d attachment(s) into worktree",
            ticket.identifier, copied,
        )


def _persist_ticket_media(ticket) -> None:
    """Gap-10 wire-in: stash image attachments as an AFM
    ``Observation_v2`` with ``media_refs`` populated.

    Vision sub #6 attaches images for the run, but the bytes vanished
    once the ADK session torn down. Capturing the paths here gives a
    durable record so future tickets can recall "we saw screenshot X
    last time" via the same memory_block path the Doer already reads.

    Soft-fail — never raises into the ticket loop. ``AIFORGE_VISION_PERSIST=0``
    opts out.
    """
    if os.environ.get("AIFORGE_VISION_PERSIST", "1") in ("0", "false", ""):
        return
    md = ticket.metadata or {}
    files = md.get("attached_files") or []
    media_paths = [
        str(f.get("path", "")) for f in files
        if isinstance(f, dict)
        and str(f.get("name", "")).lower().endswith(
            (".png", ".jpg", ".jpeg", ".gif", ".webp"),
        )
    ]
    media_paths = [p for p in media_paths if p]
    if not media_paths:
        return
    if not ticket.project:
        return

    _media_summary = (
        f"Ticket {ticket.identifier} included "
        f"{len(media_paths)} image attachment(s): "
        + ", ".join(p.rsplit("/", 1)[-1] for p in media_paths)
    )

    # Embedded (zero-infra) path — every sibling writer branches here on the
    # default SQLite backend. Without this, the attachment observation was
    # sent straight to bolt://…:7687 (fails/ImportErrors, swallowed) and the
    # Doer never recalled prior screenshots. Mirror failure_memory's idiom.
    # M5: the backend_select import + embedded() probe live INSIDE the try so
    # a backend hiccup can't raise into the ticket loop ("never raises").
    try:
        from aiforge_core.memory import backend_select as _bsel
        if _bsel.embedded():
            from aiforge_core.memory import sqlite_memory as _sqlmem
            _sqlmem.write_unit(
                text=_media_summary, kind="attachment", source="adk_runner",
                tags=[f"ticket:{ticket.identifier}", "kind:vision"],
                repo=ticket.project, ticket=ticket.identifier,
                metadata={"media_refs": media_paths},
            )
            return
    except Exception as exc:  # noqa: BLE001
        log.debug("vision persist[sqlite] failed: %s", exc)
        return

    try:
        from aiforge_memory.features.memory.store import upsert_observation
        from neo4j import GraphDatabase
    except ImportError:
        return
    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get(
        "AIFORGE_NEO4J_PASSWORD",
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        log.debug("vision persist driver fail: %s", exc)
        return
    try:
        try:
            from aiforge_core.tickets.store import get as ticket_get
            t = ticket_get(ticket.identifier)
            created_at = getattr(t, "created_at", None)
        except Exception:
            created_at = None
        event_time = None
        if created_at is not None:
            try:
                event_time = created_at.timestamp()
            except Exception:
                event_time = None
        _media_text = _media_summary
        # embed so the observation is reachable via vector recall / PPR
        # (was write-only without embed_vec). Soft on sidecar absence.
        _media_vec = None
        try:
            from aiforge_core.memory.embed import embed as _embed
            _media_vec = _embed(_media_text)
        except Exception:  # noqa: BLE001
            _media_vec = None
        upsert_observation(
            drv, repo=ticket.project,
            text=_media_text,
            kind="attachment",
            author="adk_runner",
            tags=[f"ticket:{ticket.identifier}", "kind:vision"],
            media_refs=media_paths,
            event_time=event_time,
            embed_vec=_media_vec,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("vision persist failed: %s", exc)
    finally:
        try:
            drv.close()
        except Exception:
            pass
