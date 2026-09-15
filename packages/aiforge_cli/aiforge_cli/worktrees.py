"""Two chats in one repository, without two agents in one checkout.

Different repositories need nothing from this module: a different folder is a
different session already. The same repository is the hard case — two agents
editing one working tree will overwrite each other — and the answer every
terminal agent has settled on is a git worktree per task.

Git runs INSIDE the sandbox, not on the host. The box has git and sees the repo
at the same path, so the host keeps needing nothing but docker. A worktree
created under the repo root also needs no new mount, which means no container
restart and no interruption for anyone else on the machine.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

WORKTREE_DIR = ".worktrees"
BRANCH_PREFIX = "wt/"


class GitError(Exception):
    """git said no. The message is git's own, which is usually the fix."""


@dataclass(frozen=True)
class Worktree:
    path: str
    branch: str
    head: str

    @property
    def name(self) -> str:
        return Path(self.path).name


def parse_list(porcelain: str) -> list[Worktree]:
    """``git worktree list --porcelain`` to records.

    Parsed rather than scraped from the human format, which aligns columns and
    truncates. A detached worktree has no `branch` line at all.
    """
    out: list[Worktree] = []
    path = head = branch = ""
    for raw in porcelain.splitlines() + [""]:
        line = raw.strip()
        if not line:
            if path:
                out.append(Worktree(path=path, branch=branch or "(detached)", head=head))
            path = head = branch = ""
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            path = value
        elif key == "HEAD":
            head = value[:12]
        elif key == "branch":
            branch = value.removeprefix("refs/heads/")
    return out


def worktree_path(repo: str, name: str) -> str:
    """Where a named worktree lives: inside the repo, so inside its mount."""
    return str(Path(repo) / WORKTREE_DIR / name)


def branch_name(name: str) -> str:
    return f"{BRANCH_PREFIX}{name}"


def safe_name(name: str) -> str | None:
    """None if this name cannot be a folder and a branch. Returns the reason."""
    if not name or name.startswith((".", "-")):
        return "needs a name that does not start with '.' or '-'"
    bad = set(' \t~^:?*[]\\/"\'')
    if any(c in bad for c in name):
        return "needs a name without spaces, slashes or git's refname characters"
    return None


class Git:
    """git, run wherever it exists — in the box first, on the host second.

    The box is preferred because it is guaranteed to have git and to see the
    repo at the path the session will use. The host fallback exists for a
    developer whose box is down, and is skipped silently when there is no git.
    """

    def __init__(self, *, runner=None, container: str = "aiforge",
                 host_git: str | None = "git", as_user: str | None = None):
        self._run = runner or _run
        self.container = container
        self.host_git = host_git
        # `docker exec` defaults to the image's user, which is root: a worktree
        # created that way leaves root-owned refs in .git, and the person whose
        # repo it is can no longer remove the branch or the folder. Act as the
        # host uid, exactly as the app inside the box does.
        self.as_user = as_user if as_user is not None else _host_user()

    def __call__(self, repo: str, *args: str) -> str:
        docker = ["docker", "exec"]
        if self.as_user:
            docker += ["-u", self.as_user]
        docker += ["-w", repo, self.container, "git", *args]
        code, out = self._run(docker)
        if code == 0:
            return out
        box_error = out
        if self.host_git:
            code, out = self._run([self.host_git, "-C", repo, *args])
            if code == 0:
                return out
        raise GitError(_first_useful_line(box_error or out))

    # ── operations ─────────────────────────────────────────────────────────

    def repo_root(self, cwd: str) -> str | None:
        try:
            root = self(cwd, "rev-parse", "--show-toplevel").strip()
        except GitError:
            return None
        return root or None

    def list(self, repo: str) -> list[Worktree]:
        return parse_list(self(repo, "worktree", "list", "--porcelain"))

    def add(self, repo: str, name: str, *, branch: str | None = None) -> Worktree:
        """Create ``<repo>/.worktrees/<name>`` on its own branch.

        An existing branch is checked out rather than recreated, so
        `aiforge worktree fix-retry` twice lands in the same place instead of
        failing on "branch already exists".
        """
        why = safe_name(name)
        if why is not None:
            raise GitError(f"'{name}' {why}")
        path = worktree_path(repo, name)
        wanted = branch or branch_name(name)
        existing = {w.path: w for w in self.list(repo)}
        if path in existing:
            return existing[path]
        args = ["worktree", "add"]
        if self._branch_exists(repo, wanted):
            args += [path, wanted]
        else:
            args += ["-b", wanted, path]
        self(repo, *args)
        return next(w for w in self.list(repo) if w.path == path)

    def remove(self, repo: str, name: str, *, force: bool = False) -> None:
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        self(repo, *args, worktree_path(repo, name))

    def _branch_exists(self, repo: str, branch: str) -> bool:
        try:
            self(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
            return True
        except GitError:
            return False


def _host_user() -> str:
    """``uid:gid`` of whoever ran the CLI, or "" where that has no meaning."""
    try:
        return f"{os.getuid()}:{os.getgid()}"
    except AttributeError:            # pragma: no cover - Windows
        return ""


def _run(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return p.returncode, (p.stdout + p.stderr).strip()


def _first_useful_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("hint:"):
            return line
    return text.strip() or "git failed"
