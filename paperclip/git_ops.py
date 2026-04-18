"""Git operations — scoped per role, enforced via Paperclip permissions.

Role scope (per DESIGN §5.2):
  - tester:        branch + commit (tests/** only)
  - sr-developer:  branch + commit (src/** only)
  - sr-architect:  create_mr (read-only otherwise)
  - em:            none

Each op runs `git ...` in a subprocess with cwd=repo_root. All paths are
validated against `security/file-access-rules.yml` before staging.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .permissions import PermissionDenied, file_access, role_can


class GitError(RuntimeError):
    pass


@dataclass
class GitOps:
    repo_root: Path
    git_bin: str = "git"

    # ---------- helpers ----------
    def _run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        cmd = [self.git_bin, *args]
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(self.repo_root))
        if check and r.returncode != 0:
            raise GitError(f"git {args[0]} failed: {r.stderr.strip()}")
        return r

    def status(self) -> str:
        return self._run(["status", "--short"]).stdout.strip()

    def current_branch(self) -> str:
        return self._run(["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    # ---------- branch ----------
    def branch(self, role: str, name: str) -> dict:
        """Create or switch to a branch. Allowed: tester, sr-developer."""
        if role not in ("tester", "sr-developer"):
            raise PermissionDenied(f"role={role} cannot create branches")
        # Try switch; if branch missing, create from current HEAD.
        r = self._run(["switch", name], check=False)
        if r.returncode != 0:
            self._run(["switch", "-c", name])
        return {"branch": self.current_branch()}

    # ---------- commit ----------
    def commit(self, role: str, paths: list[str], message: str) -> dict:
        """Stage given paths (validated against role write ACL) + commit."""
        if not role_can(self.repo_root, role, "git_commit"):
            raise PermissionDenied(f"role={role} cannot git_commit")
        if not paths:
            raise ValueError("paths is empty")
        bad = [p for p in paths if not file_access(self.repo_root, role, "write", p)]
        if bad:
            raise PermissionDenied(f"role={role} cannot write: {bad}")
        # Stage only the requested paths (never `git add .`).
        self._run(["add", "--", *paths])
        # Commit. `--allow-empty=false` is default; let git decide via its error.
        r = self._run(["commit", "-m", message, "--", *paths], check=False)
        if r.returncode != 0:
            raise GitError(r.stderr.strip() or r.stdout.strip())
        sha = self._run(["rev-parse", "HEAD"]).stdout.strip()
        return {"commit": sha, "message": message, "paths": paths}

    # ---------- push ----------
    def push(self, role: str, branch: str, *, remote: str = "origin") -> dict:
        if not role_can(self.repo_root, role, "git_commit"):
            raise PermissionDenied(f"role={role} cannot push")
        self._run(["push", "-u", remote, branch])
        return {"pushed": branch, "remote": remote}

    # ---------- diff ----------
    def diff(self, role: str, revspec: str = "HEAD") -> str:
        # Read-only — every role allowed to read diffs for review.
        return self._run(["diff", revspec]).stdout

    # ---------- create_mr ----------
    def create_mr(
        self,
        role: str,
        *,
        title: str,
        body: str,
        source_branch: str,
        target_branch: str = "main",
    ) -> dict:
        """Create a pull/merge request. Allowed: sr-architect only.

        Uses the `gh` CLI if available. Otherwise returns an intent record
        (caller must log & escalate to human).
        """
        if not role_can(self.repo_root, role, "git_create_mr"):
            raise PermissionDenied(f"role={role} cannot create_mr")

        if shutil.which("gh") is None:
            return {
                "ok": False,
                "reason": "gh_cli_missing",
                "intent": {"title": title, "source": source_branch, "target": target_branch},
            }
        r = subprocess.run(
            ["gh", "pr", "create",
             "--base", target_branch,
             "--head", source_branch,
             "--title", title,
             "--body", body],
            capture_output=True, text=True, cwd=str(self.repo_root),
        )
        if r.returncode != 0:
            raise GitError(f"gh pr create failed: {r.stderr.strip()}")
        return {"ok": True, "url": r.stdout.strip()}
