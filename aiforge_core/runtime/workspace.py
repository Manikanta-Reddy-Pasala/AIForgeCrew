"""Per-ticket workspace resolver.

Extracted from the legacy :mod:`aiforge_core.cli.orchestrator` so the
production adk_runner can call it without dragging the orchestrator's
dead import graph (cli.tickets / cli.roles / cli.tools) along with it.

The resolver does three things:

1. Map ``ticket.project`` (and a fallback substring scan of title+body)
   to a directory under ``WORKTREE_ROOT`` (default ``~/codeRepo``).
2. ``git worktree add`` a per-parent-ticket worktree at
   ``<repo>/.aiforge-worktrees/<ROOT_TICKET_ID>``, branched off the
   repo's default branch.
3. Persist the chosen branch back onto ``tickets.branch`` so child
   tickets re-use the same tree.

Returns the absolute worktree path or ``None`` when no safe target
repo can be identified (caller blocks the ticket).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess

from aiforge_core.config import repo_map
from aiforge_core.tickets import store as tickets

log = logging.getLogger(__name__)

# Hard rule: never run a ticket against the orchestrator's own source —
# would scribble edits onto the AIForgeCrew working tree.
_FORBIDDEN_REPOS = {"AIForgeCrew"}


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "ticket"


def _is_git_dir(path: str) -> bool:
    return bool(path) and os.path.isdir(os.path.join(path, ".git"))


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _safe_repo(path: str | None) -> str | None:
    """``path`` if it is a real git dir and not a forbidden repo, else None."""
    if path and os.path.basename(path.rstrip("/")) in _FORBIDDEN_REPOS:
        return None
    return path if _is_git_dir(path or "") else None


def _base_dirs(root: str) -> list[str]:
    try:
        return [d for d in os.listdir(root)
                if os.path.isdir(os.path.join(root, d))
                and not d.startswith(".") and d not in _FORBIDDEN_REPOS]
    except OSError:
        return []


def _by_slug(project: str, root: str, dirs: list[str]) -> str | None:
    """Case-insensitive / slug match of ``project`` against the base dirs."""
    pn = _norm(project)
    for d in dirs:
        if _norm(d) == pn and (hit := _safe_repo(os.path.join(root, d))):
            return hit
    return None


def _by_memory_source(project: str) -> str | None:
    """A registered memory source (repo) named like the project."""
    try:
        from aiforge_core.runtime import memory_sources as _ms
        for s in _ms.list_sources():
            if s.get("kind") == "repo" and _norm(s.get("name", "")) == _norm(project):
                if (hit := _safe_repo(s.get("location"))):
                    return hit
    except Exception:  # noqa: BLE001
        pass
    return None


def _by_text_scan(text: str, root: str, dirs: list[str]) -> str | None:
    """Substring / slug scan of the free text (title+body) — last resort."""
    tnorm = _norm(text)
    for d in sorted(dirs, key=len, reverse=True):
        if d in text or (len(_norm(d)) >= 4 and _norm(d) in tnorm):
            if (hit := _safe_repo(os.path.join(root, d))):
                return hit
    return None


def resolve_repo_dir(project: str, text: str = "") -> str | None:
    """Resolve a repo NAME (+ optional free text to scan) to an absolute repo
    directory. Resolution order, first hit wins:

      1. Explicit per-repo path map (``repos.json`` — set from chat/API).
      2. ``<default_root>/<project>`` exact.
      3. Case-insensitive / slug match of ``project`` against dirs in the base.
      4. A registered memory source (repo) whose name matches ``project``.
      5. Substring/slug scan of ``text`` against dir names (last resort).

    Returns None when nothing safe matches. ``AIForgeCrew`` is never returned.
    """
    project = (project or "").strip()
    root = repo_map.default_root()
    dirs = _base_dirs(root)
    candidates = []
    if project:
        candidates = [
            lambda: _safe_repo(repo_map.get_path(project)),
            lambda: _safe_repo(os.path.join(root, project)),
            lambda: _by_slug(project, root, dirs),
            lambda: _by_memory_source(project),
        ]
    if text:
        candidates.append(lambda: _by_text_scan(text, root, dirs))
    for resolve in candidates:
        if (hit := resolve()):
            return hit
    return None


def _detect_default_branch(repo_dir: str) -> str:
    proc = subprocess.run(
        ["git", "symbolic-ref", "refs/remotes/origin/HEAD"],
        cwd=repo_dir, check=False, capture_output=True,
    )
    if proc.returncode == 0:
        ref = proc.stdout.decode("utf-8", "replace").strip()
        if "/" in ref:
            return ref.rsplit("/", 1)[1]
    for candidate in ("main", "master"):
        probe = subprocess.run(
            ["git", "rev-parse", "--verify", f"origin/{candidate}"],
            cwd=repo_dir, check=False, capture_output=True,
        )
        if probe.returncode == 0:
            return candidate
    return "master"


def _resolve_repo_dir_for_ticket(ticket) -> str | None:
    return resolve_repo_dir(
        getattr(ticket, "project", "") or "",
        f"{getattr(ticket, 'title', '') or ''}\n{getattr(ticket, 'body', '') or ''}")


def _root_ticket(ticket):
    """Walk to the top of the parent chain — the branch is named for it."""
    root = ticket
    while root.parent_id:
        p = tickets.get(root.parent_id)
        if p is None:
            break
        root = p
    return root


def _branch_name(ticket, parent_ident: str) -> str:
    if ticket.branch:
        return ticket.branch
    parent = tickets.get(ticket.parent_id) if ticket.parent_id else ticket
    return f"aiforge/{parent_ident}-{_slugify(parent.title if parent else ticket.title)}"


def _fetch_origin(repo_dir: str) -> None:
    try:
        subprocess.run(["git", "fetch", "origin"], cwd=repo_dir, check=False,
                       capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        pass


def _worktree_timeout() -> int:
    try:
        return int(os.environ.get("AIFORGE_WORKTREE_TIMEOUT_S", "120"))
    except ValueError:
        return 120


def _create_worktree(repo_dir: str, repo_name: str, branch: str,
                     worktree_path: str) -> bool:
    """Add the worktree at ``worktree_path``. False when it could not be made.

    The add is BOUNDED: a stale index.lock or a hung FS would otherwise hang the
    runner indefinitely with the ticket already 'in_progress' (the sibling fetch
    is already bounded). Env-tunable; on timeout the caller blocks the ticket
    instead of hanging.
    """
    os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
    _fetch_origin(repo_dir)
    base = f"origin/{_detect_default_branch(repo_dir)}"
    timeout = _worktree_timeout()
    try:
        proc = subprocess.run(
            ["git", "worktree", "add", "-B", branch, worktree_path, base],
            cwd=repo_dir, check=False, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("worktree.timeout repo=%s branch=%s after=%ss",
                    repo_name, branch, timeout)
        return False
    if proc.returncode != 0 or not os.path.isdir(worktree_path):
        err = (proc.stderr or b"").decode("utf-8", "replace")[:500]
        log.warning("worktree.failed repo=%s branch=%s err=%s",
                    repo_name, branch, err)
        return False
    return True


def _reset_reused_worktree(repo_dir: str, repo_name: str, branch: str,
                           worktree_path: str) -> None:
    """This worktree already exists from a prior run (a child ticket sharing
    parent_ident, or a re-run of this ticket). Without a reset it keeps the
    prior task's uncommitted files AND any commits ahead of base, so this ticket
    would re-ship someone else's work as its own PR.

    Best-effort + bounded; on failure we proceed (git_pr's diff still guards,
    but the reset is what makes the reuse correct). Set
    AIFORGE_WORKTREE_REUSE_RESET=0 to opt out (e.g. deliberately resuming a
    partially-built ticket).
    """
    if os.environ.get("AIFORGE_WORKTREE_REUSE_RESET", "1") in ("0", "false"):
        return
    _fetch_origin(repo_dir)
    base = f"origin/{_detect_default_branch(repo_dir)}"
    for cmd in (["git", "checkout", "-B", branch, base],
                ["git", "reset", "--hard", base],
                ["git", "clean", "-fd"]):
        try:
            subprocess.run(cmd, cwd=worktree_path, check=False,
                           capture_output=True, timeout=60)
        except subprocess.TimeoutExpired:
            log.warning("worktree.reuse-reset timeout repo=%s cmd=%s",
                        repo_name, cmd[1])
            return


def _persist_branch(ticket, branch: str) -> None:
    if ticket.branch == branch:
        return
    try:
        # Use the public store API — `tickets` is the store MODULE, which has no
        # `_conn` (that lives on the backend classes); the old raw-SQL call
        # raised AttributeError that a bare except swallowed, so the branch was
        # NEVER persisted and child tickets kept re-creating branches.
        tickets.set_branch(ticket.id, branch)
    except Exception as exc:  # noqa: BLE001
        log.warning("branch persist failed ticket=%s: %s",
                    ticket.identifier, exc)


def ensure_branch_and_worktree(ticket) -> str | None:
    """Return absolute worktree path or ``None`` when no target repo."""
    root = _root_ticket(ticket)
    branch = _branch_name(ticket, root.identifier)
    repo_dir = (_resolve_repo_dir_for_ticket(ticket)
                or _resolve_repo_dir_for_ticket(root))
    if not repo_dir:
        return None
    # resolve_repo_dir already verified it's a git dir
    repo_name = os.path.basename(repo_dir.rstrip("/"))
    if repo_name in _FORBIDDEN_REPOS:
        return None

    worktree_path = os.path.join(repo_dir, ".aiforge-worktrees", root.identifier)
    if os.path.isdir(worktree_path):
        _reset_reused_worktree(repo_dir, repo_name, branch, worktree_path)
    elif not _create_worktree(repo_dir, repo_name, branch, worktree_path):
        return None
    _persist_branch(ticket, branch)
    return worktree_path


__all__ = ["ensure_branch_and_worktree", "resolve_repo_dir"]
