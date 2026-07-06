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

from aiforge_core.config.env import WORKTREE_ROOT
from aiforge_core.tickets import store as tickets

log = logging.getLogger(__name__)

# Hard rule: never run a ticket against the orchestrator's own source —
# would scribble edits onto the AIForgeCrew working tree.
_FORBIDDEN_REPOS = {"AIForgeCrew"}


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:40] or "ticket"


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


def _infer_repo_from_ticket(ticket) -> str | None:
    project = (ticket.project or "").strip()
    if project and project not in _FORBIDDEN_REPOS and \
            os.path.isdir(os.path.join(WORKTREE_ROOT, project)):
        return project
    try:
        all_dirs = [
            d for d in os.listdir(WORKTREE_ROOT)
            if os.path.isdir(os.path.join(WORKTREE_ROOT, d))
            and not d.startswith(".")
            and d not in _FORBIDDEN_REPOS
        ]
    except OSError:
        all_dirs = []
    all_dirs.sort(key=len, reverse=True)
    text = f"{ticket.title or ''}\n{ticket.body or ''}"
    for d in all_dirs:
        if d in text:
            return d
    return None


def ensure_branch_and_worktree(ticket) -> str | None:
    """Return absolute worktree path or ``None`` when no target repo."""
    root = ticket
    while root.parent_id:
        p = tickets.get(root.parent_id)
        if p is None:
            break
        root = p
    parent_ident = root.identifier

    existing = ticket.branch
    if existing:
        branch = existing
    else:
        parent = tickets.get(ticket.parent_id) if ticket.parent_id else ticket
        slug = _slugify(parent.title if parent else ticket.title)
        branch = f"aiforge/{parent_ident}-{slug}"

    repo_name = _infer_repo_from_ticket(ticket) or _infer_repo_from_ticket(root)
    if not repo_name or repo_name == "AIForgeCrew":
        return None
    repo_dir = os.path.join(WORKTREE_ROOT, repo_name)
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return None

    worktree_path = os.path.join(repo_dir, ".aiforge-worktrees", parent_ident)
    if not os.path.isdir(worktree_path):
        os.makedirs(os.path.dirname(worktree_path), exist_ok=True)
        try:
            subprocess.run(
                ["git", "fetch", "origin"], cwd=repo_dir,
                check=False, capture_output=True, timeout=60,
            )
        except subprocess.TimeoutExpired:
            pass
        default_branch = _detect_default_branch(repo_dir)
        base = f"origin/{default_branch}"
        # Bound the add: a stale index.lock or a hung FS would otherwise hang
        # the runner indefinitely with the ticket already 'in_progress' (the
        # sibling fetch above is already bounded). Env-tunable; on timeout,
        # bail to None so the caller blocks the ticket instead of hanging.
        try:
            _wt_timeout = int(os.environ.get("AIFORGE_WORKTREE_TIMEOUT_S", "120"))
        except ValueError:
            _wt_timeout = 120
        try:
            proc = subprocess.run(
                ["git", "worktree", "add", "-B", branch, worktree_path, base],
                cwd=repo_dir, check=False, capture_output=True,
                timeout=_wt_timeout,
            )
        except subprocess.TimeoutExpired:
            log.warning("worktree.timeout repo=%s branch=%s after=%ss",
                        repo_name, branch, _wt_timeout)
            return None
        if proc.returncode != 0 or not os.path.isdir(worktree_path):
            err = (proc.stderr or b"").decode("utf-8", "replace")[:500]
            log.warning(
                "worktree.failed repo=%s branch=%s err=%s",
                repo_name, branch, err,
            )
            return None
    else:
        # REUSE: this worktree already exists from a prior run (a child ticket
        # sharing parent_ident, or a re-run of this ticket). Without a reset it
        # keeps the prior task's uncommitted files AND any commits ahead of base,
        # so this ticket would re-ship someone else's work as its own PR. Reset
        # it to a clean base branch before the Doer touches it. Best-effort +
        # bounded; on failure we proceed (git_pr's diff still guards, but the
        # reset is what makes the reuse correct). Set AIFORGE_WORKTREE_REUSE_RESET=0
        # to opt out (e.g. deliberately resuming a partially-built ticket).
        if os.environ.get("AIFORGE_WORKTREE_REUSE_RESET", "1") not in ("0", "false"):
            try:
                subprocess.run(["git", "fetch", "origin"], cwd=repo_dir,
                               check=False, capture_output=True, timeout=60)
            except subprocess.TimeoutExpired:
                pass
            _base = f"origin/{_detect_default_branch(repo_dir)}"
            for _cmd in (["git", "checkout", "-B", branch, _base],
                         ["git", "reset", "--hard", _base],
                         ["git", "clean", "-fd"]):
                try:
                    subprocess.run(_cmd, cwd=worktree_path, check=False,
                                   capture_output=True, timeout=60)
                except subprocess.TimeoutExpired:
                    log.warning("worktree.reuse-reset timeout repo=%s cmd=%s",
                                repo_name, _cmd[1])
                    break

    if ticket.branch != branch:
        try:
            # Use the public store API — `tickets` is the store MODULE, which has
            # no `_conn` (that lives on the backend classes); the old raw-SQL call
            # raised AttributeError that the bare except swallowed, so the branch
            # was NEVER persisted and child tickets kept re-creating branches.
            tickets.set_branch(ticket.id, branch)
        except Exception as exc:  # noqa: BLE001
            log.warning("branch persist failed ticket=%s: %s",
                        ticket.identifier, exc)
    return worktree_path


__all__ = ["ensure_branch_and_worktree"]
