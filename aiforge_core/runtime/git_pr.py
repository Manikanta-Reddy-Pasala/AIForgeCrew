"""Auto-commit + push + open-PR helper for the v6 ticket runner.

Lives separate from ``adk_runner`` so the orchestrator stays focused on
ticket lifecycle. Two public entry points:

* :func:`run_git`              — thin subprocess wrapper (5-min cap).
* :func:`commit_push_open_pr`  — full happy/sad path for the runner.

The PR step short-circuits cleanly when:

* the workspace isn't a git repo
* the working tree has no Doer-authored changes (transient cache dirs
  excluded — see ``_EXCLUDE_PATHSPECS``)
* ``gh`` CLI isn't installed (push still happens, PR step is skipped
  with a logged hint)

Returns a metadata patch dict the runner merges into ticket metadata
so the human triage loop sees ``pr_url``, ``branch_pushed``, and
``pr_skip_reason`` together.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess


log = logging.getLogger("aiforge.git_pr")


# Transient dirs the runner / sidecars write into the workspace. Must
# NOT land in PRs even when the target repo's own .gitignore doesn't
# cover them (most don't — graphify-out came in via the AIForgeCrew
# convention, not the target repo's). Used at status + add time so
# the diff stays scoped to real Doer work.
_EXCLUDE_PATHSPECS: tuple[str, ...] = (
    ":(exclude)graphify-out",
    ":(exclude).aiforge",
    ":(exclude).aiforge-worktrees",
    ":(exclude).idea",
    ":(exclude).vscode",
    ":(exclude).DS_Store",
    # Python bytecode + caches — slipped into TallyConnector#10/#11
    # because those repos lacked a Python .gitignore. Belt-and-braces
    # at git add time so the target repo's own ignores are irrelevant.
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.py[cod]",
    ":(exclude,glob)**/*.class",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/.ruff_cache/**",
    ":(exclude,glob)**/.mypy_cache/**",
)


# File-path heuristics for classifying a diff as test-only. Conservative:
# anything that is clearly NOT a test counts as a production change and
# unblocks the PR. False negatives (calling real prod code a "test") are
# worse than false positives — they let a fix-less PR ship.
_TEST_PATH_FRAGMENTS = (
    "/src/test/", "src/test/",          # Java/Maven layout
    "/test/", "/tests/",                # Python / Go / generic
    "/__tests__/",                      # JS/TS Jest
    "/fixtures/", "/testdata/",
    "/spec/",                           # Ruby / some JS
)
_TEST_SUFFIXES = (
    "_test.py", "_test.go", "_test.ts", "_test.tsx",
    ".test.ts", ".test.tsx", ".test.js", ".test.jsx",
    "test.java",                         # *Test.java / FooTest.java
    "_spec.rb",
)
_TEST_ALWAYS_PATHS = (
    "__pycache__/", ".pytest_cache/", ".ruff_cache/", ".mypy_cache/",
)


def _is_test_path(path: str) -> bool:
    p = path.lower()
    # Normalise so leading-segment matches work for both ``tests/foo``
    # and ``./tests/foo``-style git outputs.
    norm = "/" + p.lstrip("./")
    if any(p.startswith(t) or t in p for t in _TEST_ALWAYS_PATHS):
        return True
    if any(frag in norm for frag in _TEST_PATH_FRAGMENTS):
        return True
    if any(p.endswith(suf) for suf in _TEST_SUFFIXES):
        return True
    return False


def _classify_head_diff(repo_root: str) -> tuple[list[str], list[str]]:
    """Return ``(prod_files, test_files)`` changed in HEAD's commit.

    Uses ``git diff-tree`` so it works on the just-created Doer commit
    even before push. Returns two empty lists when nothing changed or
    the command fails — the caller treats that as "skip the guard"
    rather than blocking on a tooling hiccup.
    """
    rc, out, _ = run_git(
        ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"],
        repo_root,
    )
    if rc != 0 or not out.strip():
        return [], []
    prod, test = [], []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        (test if _is_test_path(line) else prod).append(line)
    return prod, test


def run_git(args: list[str], cwd: str) -> tuple[int, str, str]:
    """Run a git/gh command and capture stdout/stderr.

    Returns ``(returncode, stdout[:1000], stderr[:1000])``. Hard 5-min
    timeout per call so a hung remote can't stall the runner.
    """
    proc = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=300,
    )
    return proc.returncode, (proc.stdout or "")[:1000], (proc.stderr or "")[:1000]


def _resolve_repo_root() -> str | None:
    """Honour ``AIFORGE_REPO_ROOT`` and confirm it's a git repo.

    Accepts both regular repos (``.git`` is a directory) and worktrees
    (``.git`` is a file containing ``gitdir: ...``). Falls back to
    ``git rev-parse --git-dir`` so any layout git itself accepts also
    works here.
    """
    repo_root = os.path.expanduser(os.environ.get(
        "AIFORGE_REPO_ROOT", "~/aiforge_workspace",
    ))
    dot_git = os.path.join(repo_root, ".git")
    if os.path.isdir(dot_git) or os.path.isfile(dot_git):
        return repo_root
    probe = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=repo_root if os.path.isdir(repo_root) else None,
        check=False, capture_output=True,
    )
    if probe.returncode == 0:
        return repo_root
    log.warning("git_pr.skip: %s is not a git repo", repo_root)
    return None


def _default_base_branch(repo_root: str) -> str:
    """Resolve the upstream base branch. Tries `origin/HEAD` first
    (the GitHub default branch the operator set in repo settings),
    falls back to `origin/master` then `origin/main`. Returns the
    first ref that exists; returns `origin/master` as a sane default
    when nothing resolves (push will fail later with a clear error)."""
    rc, out, _ = run_git(
        ["git", "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"],
        repo_root,
    )
    if rc == 0 and out.strip():
        # `refs/remotes/origin/master` -> `origin/master`
        return out.strip().replace("refs/remotes/", "")
    for candidate in ("origin/master", "origin/main"):
        rc, _, _ = run_git(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            repo_root,
        )
        if rc == 0:
            return candidate
    return "origin/master"


def _has_unpushed_commits(repo_root: str) -> tuple[bool, str]:
    """``(True, base)`` when HEAD is ahead of the upstream base by at
    least one commit. ``(False, reason)`` otherwise. Used by
    :func:`_has_doer_changes` to detect the Doer-self-committed path:
    PR #22's ``git_commit`` tool lets the Doer commit milestones
    in-loop, leaving the working tree clean by the time
    ``commit_push_open_pr`` runs — without this check, the runner
    short-circuits on ``no_changes`` and the work never gets pushed."""
    base = _default_base_branch(repo_root)
    rc, out, _ = run_git(
        ["git", "rev-list", "--count", f"{base}..HEAD"], repo_root,
    )
    if rc != 0:
        return False, "rev_list_failed"
    try:
        ahead = int((out or "0").strip())
    except ValueError:
        ahead = 0
    if ahead > 0:
        return True, base
    return False, "head_at_base"


def _has_doer_changes(repo_root: str) -> tuple[bool, str]:
    """``(True, "")`` when EITHER the working tree has uncommitted
    Doer-authored changes OUTSIDE the transient-dir allowlist, OR
    HEAD is ahead of the upstream base (Doer self-committed via
    PR #22's ``git_commit`` tool). ``(False, reason)`` otherwise."""
    rc, out, err = run_git(
        ["git", "status", "--porcelain", "--", ".", *_EXCLUDE_PATHSPECS],
        repo_root,
    )
    if rc != 0:
        log.warning("git_pr.status_failed: %s", err)
        return False, "git_status_failed"
    if out.strip():
        return True, ""
    # Working tree clean — but the Doer may have committed in-loop.
    # Detect unpushed commits ahead of upstream base.
    ahead, info = _has_unpushed_commits(repo_root)
    if ahead:
        log.info("git_pr.unpushed: HEAD ahead of %s — will push", info)
        return True, ""
    log.info("git_pr.clean: no Doer changes (transient dirs excluded)")
    return False, "no_changes"


def _checkout_branch(repo_root: str, branch: str) -> str:
    """Create or reset the ticket branch to current HEAD. Returns ``""``
    on success or a ``pr_skip_reason`` on failure.

    Uses ``checkout -B`` (capital B) which CREATES the branch when
    absent OR FORCE-RESETS it to current HEAD when it exists. Beats
    the old ``branch -D && checkout -b`` pair which fails when HEAD
    is already on the target branch (delete blocked + create errors
    'already exists') — that's the exact failure that bricked the
    ONE-117 retry run after MLX crashed mid-pipeline.
    """
    rc, _, err = run_git(["git", "checkout", "-B", branch], repo_root)
    if rc != 0:
        log.warning("git_pr.checkout_failed: %s", err)
        return "checkout_failed"
    return ""


_DEFAULT_GITIGNORE = """\
# Auto-added by AIForgeCrew runtime so Doer-emitted artefacts don't
# pollute commits. Edit freely; runtime only writes this file when it
# does not already exist.
__pycache__/
*.py[cod]
*$py.class
.pytest_cache/
.ruff_cache/
.mypy_cache/
.tox/
.coverage
htmlcov/
*.egg-info/
build/
dist/

# Local databases / scratch
*.db
*.sqlite
*.sqlite3

# Virtualenv
.venv/
venv/
env/

# Editor / OS
.idea/
.vscode/
.DS_Store

# Node (when Doer scaffolds JS)
node_modules/

# Java (when Doer scaffolds Maven/Gradle)
target/
build/
"""


def _ensure_gitignore(repo_root: str) -> None:
    """Write a sensible default ``.gitignore`` when the repo doesn't
    already have one. Stops Doer-emitted ``__pycache__`` and SQLite
    scratch DBs from landing in commits — the stress test that
    motivated this file produced 7 ``.pyc`` files + a ``stress5k.db``
    in the snapshot before this guard existed.

    No-op when ``.gitignore`` already exists — operator's choice wins.
    """
    gi = os.path.join(repo_root, ".gitignore")
    if os.path.exists(gi):
        return
    try:
        with open(gi, "w", encoding="utf-8") as f:
            f.write(_DEFAULT_GITIGNORE)
    except OSError as exc:
        log.warning("git_pr.gitignore_write_failed: %s", exc)


def _commit_changes(repo_root: str, identifier: str, title: str) -> str:
    """Stage Doer changes (transient dirs excluded) and commit. Returns
    ``""`` on success or a ``pr_skip_reason`` on failure."""
    _ensure_gitignore(repo_root)
    run_git(
        ["git", "add", "--", ".", *_EXCLUDE_PATHSPECS],
        repo_root,
    )
    msg = (
        f"feat({identifier}): {title}\n\n"
        f"Generated by AIForgeCrew v6 pipeline."
    )
    rc, out, err = run_git(["git", "commit", "-m", msg], repo_root)
    if rc != 0 and "nothing to commit" not in (out + err):
        log.warning("git_pr.commit_failed: %s", err)
        return "commit_failed"
    return ""


def _has_reachable_remote(repo_root: str) -> tuple[bool, str]:
    """Pre-check before push: does ``origin`` exist AND is it reachable?

    Returns ``(True, '')`` when ``git ls-remote origin`` succeeds in
    ≤10s. Returns ``(False, reason)`` for: no origin configured, repo
    not on GitHub (e.g. fresh stress sandbox), DNS/auth failure.

    Catching this BEFORE ``git push`` saves the runner ~30s on every
    isolated/sandbox ticket and gives the operator a clean
    ``pr_skip_reason='no_remote'`` instead of a noisy push_failed.
    """
    rc, out, _ = run_git(["git", "remote"], repo_root)
    if rc != 0 or "origin" not in (out or "").split():
        return False, "no_origin_configured"
    try:
        proc = subprocess.run(
            ["git", "ls-remote", "--exit-code", "origin", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        return False, "remote_unreachable_timeout"
    except Exception as exc:  # noqa: BLE001
        return False, f"remote_probe_error: {exc}"
    if proc.returncode != 0:
        return False, "remote_unreachable"
    return True, ""


def _push(repo_root: str, branch: str) -> tuple[bool, str]:
    """``(pushed?, err_or_empty)``.

    Probes the remote first via :func:`_has_reachable_remote` so a
    sandbox repo without a real origin (stress tests, scratch
    worktrees) returns a clean ``no_remote`` reason instead of timing
    out ``git push``.
    """
    reachable, reason = _has_reachable_remote(repo_root)
    if not reachable:
        log.info("git_pr.push_skipped: %s", reason)
        return False, reason
    rc, _, err = run_git(
        ["git", "push", "-u", "origin", branch], repo_root,
    )
    if rc == 0:
        return True, ""
    # Re-run of a ticket: the aiforge/<id> branch already exists on
    # origin from a prior attempt and our local worktree branch has
    # been re-created from base, so the histories diverged and the
    # plain push is rejected (non-fast-forward). Since this branch is
    # exclusively ours (aiforge/* namespace, one ticket = one branch),
    # a lease-guarded force is safe — it only overwrites if the remote
    # is still where we last saw it. Never touches master/main.
    low = (err or "").lower()
    if (branch.startswith("aiforge/")
            and ("non-fast-forward" in low or "rejected" in low
                 or "fetch first" in low or "stale info" in low)):
        log.info("git_pr.push retry --force-with-lease for %s", branch)
        rc2, _, err2 = run_git(
            ["git", "push", "--force-with-lease", "-u", "origin", branch],
            repo_root,
        )
        if rc2 == 0:
            return True, ""
        log.warning("git_pr.push_failed (after lease retry): %s", err2)
        return False, err2[:300]
    log.warning("git_pr.push_failed: %s", err)
    return False, err[:300]


def _open_pr(repo_root: str, identifier: str, title: str,
             body: str) -> tuple[str, str]:
    """``(pr_url, error_or_empty)``. PR url is empty when ``gh`` is
    absent — push has already happened by then."""
    if not shutil.which("gh"):
        log.info("git_pr.gh_absent — push done, skipping PR creation")
        return "", "gh_not_installed"
    rc, out, err = run_git(
        ["gh", "pr", "create",
         "--title", f"{identifier}: {title}",
         "--body", body],
        repo_root,
    )
    if rc != 0:
        combined = f"{out}\n{err}"
        # Re-run of a ticket whose branch already has an open PR (ONE-1
        # pushed an updated fix via force-with-lease, but `gh pr create`
        # refuses a second PR for the same branch). The work IS shipped —
        # extract the existing PR URL from gh's error and treat it as
        # success so the ticket doesn't dead-end as ``blocked`` over an
        # already-delivered change.
        import re as _re
        m = _re.search(r"https://github\.com/\S+/pull/\d+", combined)
        if m:
            existing = m.group(0)
            log.info("git_pr.exists — reusing open PR %s", existing)
            return existing, ""
        # No URL in the error but the message clearly says it exists →
        # ask gh for the branch's PR url explicitly.
        if "already exists" in combined.lower():
            rc2, out2, _ = run_git(
                ["gh", "pr", "view", "--json", "url", "-q", ".url"],
                repo_root,
            )
            url2 = (out2 or "").strip().splitlines()[-1] if out2 else ""
            if rc2 == 0 and url2.startswith("http"):
                log.info("git_pr.exists — resolved open PR %s", url2)
                return url2, ""
        log.warning("git_pr.gh_create_failed: %s", err)
        return "", err[:300]
    pr_url = (out or "").strip().splitlines()[-1] if out else ""
    log.info("git_pr.opened: %s", pr_url)
    return pr_url, ""


def commit_push_open_pr(ticket) -> dict:
    """Commit Doer edits, push to origin, open a PR via ``gh`` CLI.

    Returns a metadata patch dict the caller merges into ticket
    metadata. Possible keys: ``pr_url``, ``branch_pushed``,
    ``pr_skip_reason``, ``push_err``, ``gh_err``. Empty dict on hard
    failure (caller still records the ticket as blocked).
    """
    repo_root = _resolve_repo_root()
    if repo_root is None:
        return {"pr_skip_reason": "not_a_git_repo"}

    has_changes, reason = _has_doer_changes(repo_root)
    if not has_changes:
        return {"pr_skip_reason": reason}

    branch = ticket.branch or f"aiforge/{ticket.identifier}"
    err = _checkout_branch(repo_root, branch)
    if err:
        return {"pr_skip_reason": err}

    title = (ticket.title or ticket.identifier).strip().replace("\n", " ")
    err = _commit_changes(repo_root, ticket.identifier, title)
    if err:
        return {"pr_skip_reason": err}

    # Empty-production-diff guard. ONE-3 false-positive: validator
    # approved a PR whose only changes were a JUnit test class + an XML
    # fixture, with zero edits to ``src/main``. Tests prove a fix; they
    # are not the fix. Reject so the runner can re-loop the Doer
    # instead of publishing a no-op PR.
    # Toggle: ``AIFORGE_ALLOW_TEST_ONLY_PR=1`` reinstates the old
    # behaviour for the rare valid case (e.g. backfilling regression
    # coverage with no production change).
    if os.environ.get("AIFORGE_ALLOW_TEST_ONLY_PR", "0") not in ("1", "true"):
        prod, test = _classify_head_diff(repo_root)
        if test and not prod:
            log.warning(
                "git_pr.test_only_diff: %d test file(s), no production change "
                "— rejecting PR. Tests: %s",
                len(test), ", ".join(test[:5]),
            )
            return {
                "pr_skip_reason": "test_only_diff",
                "test_only_files": test[:10],
            }

    pushed, push_err = _push(repo_root, branch)
    if not pushed:
        return {"branch_pushed": False, "pr_skip_reason": "push_failed",
                "push_err": push_err}

    pr_body = (
        f"AIForgeCrew v6 pipeline auto-generated PR for ticket "
        f"{ticket.identifier}.\n\n"
        f"## Original ticket body\n{(ticket.body or '')[:1500]}"
    )
    pr_url, pr_err = _open_pr(repo_root, ticket.identifier, title, pr_body)
    if not pr_url:
        return {"branch_pushed": True,
                "pr_skip_reason": pr_err or "gh_create_failed",
                "gh_err": pr_err}
    # Emit a `pr_opened` event so the UI's audit panel surfaces the
    # link. Best-effort: a Postgres hiccup logs but never blocks.
    try:
        from .observability import emit_pr_opened
        emit_pr_opened(ticket_id=ticket.id, pr_url=pr_url, branch=branch)
    except Exception:  # noqa: BLE001
        pass
    _fire_delta_ingest(ticket, repo_root)
    return {"branch_pushed": True, "pr_url": pr_url}


def _fire_delta_ingest(ticket, repo_root: str) -> None:
    """Fire-and-forget AiForgeMemory delta ingest of the just-pushed code.

    Without this, the code-chunk vector index only refreshes on the
    aiforge-memory scheduler daemon's interval — recall on the next
    ticket (or a re-read on THIS ticket) returns the pre-change code.
    Soft: missing CLI / missing project / disabled env all no-op.
    Toggle: AIFORGE_POST_PR_INGEST=0.
    """
    if os.environ.get("AIFORGE_POST_PR_INGEST", "1") in {"0", "false", ""}:
        return
    project = (getattr(ticket, "project", "") or "").strip()
    cli = shutil.which("aiforge-memory")
    if not project or not cli or not repo_root:
        return
    try:
        subprocess.Popen(
            [cli, "ingest", project, "--path", str(repo_root), "--delta"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("post-PR delta ingest fired repo=%s path=%s",
                 project, repo_root)
    except Exception as exc:  # noqa: BLE001
        log.debug("post-PR delta ingest failed to spawn: %s", exc)


def merge_pr(pr_url: str, *, squash: bool = True) -> dict:
    """Merge a PR after the live_verifier validated it.

    Returns ``{"merged": bool, "reason": str}``. Idempotent-ish: when
    the PR is already merged (e.g. the deploy recipe merged it for a
    ``deploy_target=qa`` ticket) ``gh`` errors and we report
    ``already_merged`` rather than treating it as a failure. Never
    touches anything but the PR's own branch.
    """
    if not pr_url:
        return {"merged": False, "reason": "no_pr_url"}
    if not shutil.which("gh"):
        return {"merged": False, "reason": "gh_not_installed"}
    args = ["gh", "pr", "merge", pr_url, "--delete-branch"]
    args.append("--squash" if squash else "--merge")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    out = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode == 0:
        log.info("git_pr.merged %s", pr_url)
        return {"merged": True, "reason": ""}
    low = out.lower()
    if "already merged" in low or "not mergeable" in low and "merged" in low:
        return {"merged": True, "reason": "already_merged"}
    log.warning("git_pr.merge_failed %s: %s", pr_url, out[:300])
    return {"merged": False, "reason": out[:300]}


__all__ = ["run_git", "commit_push_open_pr", "merge_pr"]
