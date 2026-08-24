"""Unit tests for runtime.git_pr helpers — remote reachability probe
+ default .gitignore template. No actual ``git push``."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from aiforge_core.runtime import git_pr as gp


def _git_init(tmp: Path) -> Path:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "init"],
                   cwd=tmp, check=True, capture_output=True)
    return tmp


# ─── _has_reachable_remote ────────────────────────────────────────────


def test_has_reachable_remote_no_origin(tmp_path: Path) -> None:
    _git_init(tmp_path)
    ok, reason = gp._has_reachable_remote(str(tmp_path))
    assert ok is False
    assert reason == "no_origin_configured"


def test_has_reachable_remote_unreachable_origin(tmp_path: Path) -> None:
    _git_init(tmp_path)
    subprocess.run(
        ["git", "remote", "add", "origin",
         "https://github.com/no-such-user-foo/no-such-repo-bar.git"],
        cwd=tmp_path, check=True, capture_output=True,
    )
    ok, reason = gp._has_reachable_remote(str(tmp_path))
    assert ok is False
    assert reason in ("remote_unreachable", "remote_unreachable_timeout",
                      "remote_probe_error: " + reason.split(":", 1)[-1].strip()
                      if reason.startswith("remote_probe_error") else "x")


# ─── _ensure_gitignore ───────────────────────────────────────────────


def test_ensure_gitignore_writes_when_absent(tmp_path: Path) -> None:
    gp._ensure_gitignore(str(tmp_path))
    gi = tmp_path / ".gitignore"
    assert gi.exists()
    body = gi.read_text(encoding="utf-8")
    # Spot-check the patterns that the Doer's stress run leaked.
    assert "__pycache__/" in body
    assert "*.db" in body
    assert ".venv/" in body
    assert ".DS_Store" in body


def test_ensure_gitignore_preserves_existing(tmp_path: Path) -> None:
    """Operator's existing .gitignore wins — runtime never overwrites."""
    gi = tmp_path / ".gitignore"
    gi.write_text("# operator-curated\nfoo/\n", encoding="utf-8")
    gp._ensure_gitignore(str(tmp_path))
    body = gi.read_text(encoding="utf-8")
    assert body == "# operator-curated\nfoo/\n"


def test_default_gitignore_template_covers_polyglot_artifacts() -> None:
    """Template must catch artifacts from Python/Node/Java scaffolds —
    the stress test surfaced .pyc + .db; future tickets may scaffold
    Maven (target/) or Node (node_modules/) projects."""
    body = gp._DEFAULT_GITIGNORE
    assert "__pycache__/" in body          # Python
    assert "node_modules/" in body          # Node
    assert "target/" in body                # Maven
    assert "*.db" in body                    # SQLite scratch
    assert "build/" in body                  # Gradle/Setuptools


# ─── _checkout_branch idempotent re-runs ────────────────────────────────


def test_checkout_branch_handles_existing_branch(tmp_path: Path) -> None:
    """The runner's pipeline can crash mid-run; on retry _checkout_branch
    must NOT fail when the branch already exists from the prior run.
    Uses 'checkout -B' which creates-or-resets."""
    _git_init(tmp_path)
    # Pre-create the branch (simulate prior run that crashed).
    subprocess.run(
        ["git", "branch", "ticket-foo"], cwd=tmp_path, check=True,
        capture_output=True,
    )
    reason = gp._checkout_branch(str(tmp_path), "ticket-foo")
    assert reason == ""
    # Confirm we're now on the branch.
    rc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert rc.stdout.strip() == "ticket-foo"


def test_checkout_branch_creates_when_absent(tmp_path: Path) -> None:
    _git_init(tmp_path)
    reason = gp._checkout_branch(str(tmp_path), "ticket-bar")
    assert reason == ""
    rc = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert rc.stdout.strip() == "ticket-bar"


def test_checkout_branch_resets_existing_to_head(tmp_path: Path) -> None:
    """If branch existed at an older commit, ``checkout -B`` resets it
    to current HEAD. Important when Doer's mid-pipeline-crash branch
    is N commits behind main on retry."""
    _git_init(tmp_path)
    # Make a second commit on main.
    (tmp_path / "x.txt").write_text("x")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "add", "x.txt"], cwd=tmp_path, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "second"], cwd=tmp_path, check=True,
        capture_output=True,
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
        text=True,
    ).stdout.strip()
    # Pre-create branch at the FIRST commit (older).
    subprocess.run(
        ["git", "branch", "stale", "HEAD~1"], cwd=tmp_path, check=True,
        capture_output=True,
    )
    reason = gp._checkout_branch(str(tmp_path), "stale")
    assert reason == ""
    new_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True,
        text=True,
    ).stdout.strip()
    assert new_head == head  # branch reset to current HEAD


# ─── _has_doer_changes detects unpushed commits ────────────────────────


def _make_repo_with_commit_ahead(tmp_path: Path) -> None:
    """Init a repo with an 'origin' simulating an upstream that's
    behind HEAD by one commit (the Doer-self-committed scenario)."""
    upstream = tmp_path / "upstream.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--bare", str(upstream)], check=True,
                   capture_output=True)
    subprocess.run(["git", "init", "-b", "master", str(work)], check=True,
                   capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init"],
        cwd=work, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(upstream)],
        cwd=work, check=True, capture_output=True,
    )
    subprocess.run(["git", "push", "-u", "origin", "master"],
                   cwd=work, check=True, capture_output=True)
    # Add an 'ahead' commit (simulates Doer's git_commit firing).
    (work / "x.txt").write_text("x")
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "add", "x.txt"],
        cwd=work, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "doer milestone"],
        cwd=work, check=True, capture_output=True,
    )
    return work


def test_has_doer_changes_detects_unpushed_commits(tmp_path: Path) -> None:
    """Working tree clean + HEAD ahead of origin/master = Doer
    self-committed via the new git_commit tool. _has_doer_changes
    must return True so commit_push_open_pr proceeds with the push."""
    work = _make_repo_with_commit_ahead(tmp_path)
    has, reason = gp._has_doer_changes(str(work))
    assert has is True
    assert reason == ""


def test_has_doer_changes_clean_at_base_returns_false(tmp_path: Path) -> None:
    """Working tree clean + HEAD == origin/master = nothing to ship."""
    work = _make_repo_with_commit_ahead(tmp_path)
    # Push the milestone, leaving HEAD == origin/master.
    subprocess.run(["git", "push"], cwd=work, check=True, capture_output=True)
    has, reason = gp._has_doer_changes(str(work))
    assert has is False
    assert reason == "no_changes"


def test_has_doer_changes_uncommitted_still_works(tmp_path: Path) -> None:
    """Pre-existing path: working tree dirty → True. Must still hold."""
    _git_init(tmp_path)
    (tmp_path / "y.py").write_text("y = 1")
    has, reason = gp._has_doer_changes(str(tmp_path))
    assert has is True
    assert reason == ""


def test_default_base_branch_resolves_master(tmp_path: Path) -> None:
    """When the repo has only origin/master, that's the base."""
    work = _make_repo_with_commit_ahead(tmp_path)
    base = gp._default_base_branch(str(work))
    assert base == "origin/master"


def test_default_base_branch_no_origin_fallback(tmp_path: Path) -> None:
    """Repo without origin remote returns the safe default."""
    _git_init(tmp_path)
    base = gp._default_base_branch(str(tmp_path))
    assert base == "origin/master"


# ─────────────────── empty-production-diff guard ──────────────────────


def test_is_test_path_java_maven() -> None:
    assert gp._is_test_path("src/test/java/com/x/FooTest.java") is True
    assert gp._is_test_path("src/main/java/com/x/Foo.java") is False


def test_is_test_path_python_pytest() -> None:
    assert gp._is_test_path("tests/test_thing.py") is True
    assert gp._is_test_path("pkg/foo_test.py") is True
    assert gp._is_test_path("pkg/foo.py") is False


def test_is_test_path_js_jest() -> None:
    assert gp._is_test_path("src/__tests__/foo.test.tsx") is True
    assert gp._is_test_path("src/foo.test.ts") is True
    assert gp._is_test_path("src/foo.ts") is False


def test_is_test_path_pycache_always() -> None:
    assert gp._is_test_path("__pycache__/x.cpython-312.pyc") is True
    assert gp._is_test_path("foo/__pycache__/x.pyc") is True


def test_is_test_path_strips_only_dot_slash() -> None:
    # item 7b — strip a leading "./" only; the old lstrip("./") footgun ate
    # leading dots, turning a `.test/` config dir into `test/` and wrongly
    # flagging it as a test path.
    assert gp._is_test_path("./tests/test_x.py") is True     # ./ stripped
    assert gp._is_test_path(".test/config.py") is False      # NOT a test path
    assert gp._is_test_path(".github/workflows/ci.yml") is False


def test_is_test_path_fixtures() -> None:
    assert gp._is_test_path("src/test/resources/fixtures/credit-note.xml") is True
    assert gp._is_test_path("src/main/resources/config.xml") is False


def _commit_files(repo: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
        subprocess.run(["git", "add", rel], cwd=repo, check=True,
                       capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "diff"],
        cwd=repo, check=True, capture_output=True,
    )


def test_classify_head_diff_test_only(tmp_path: Path) -> None:
    """Java PR that adds ONLY a JUnit test + XML fixture — exactly the
    ONE-3 false-positive shape — comes back as test-only."""
    _git_init(tmp_path)
    _commit_files(tmp_path, {
        "src/test/java/com/x/FooTest.java": "class FooTest{}",
        "src/test/resources/fixtures/sample.xml": "<root/>",
    })
    prod, test = gp._classify_head_diff(str(tmp_path))
    assert prod == []
    assert len(test) == 2


def test_classify_head_diff_mixed(tmp_path: Path) -> None:
    """A real fix touches src/main AND src/test — counts as prod."""
    _git_init(tmp_path)
    _commit_files(tmp_path, {
        "src/main/java/com/x/Foo.java": "class Foo{}",
        "src/test/java/com/x/FooTest.java": "class FooTest{}",
    })
    prod, test = gp._classify_head_diff(str(tmp_path))
    assert prod == ["src/main/java/com/x/Foo.java"]
    assert test == ["src/test/java/com/x/FooTest.java"]


def test_classify_head_diff_empty_when_no_commits(tmp_path: Path) -> None:
    _git_init(tmp_path)
    prod, test = gp._classify_head_diff(str(tmp_path))
    assert prod == []
    assert test == []


# ── auto-merge on validate ────────────────────────────────────────────


def test_merge_pr_no_url() -> None:
    assert gp.merge_pr("")["merged"] is False
    assert gp.merge_pr("")["reason"] == "no_pr_url"


def test_merge_pr_no_gh(monkeypatch) -> None:
    monkeypatch.setattr(gp.shutil, "which", lambda _x: None)
    out = gp.merge_pr("https://github.com/o/r/pull/1")
    assert out["merged"] is False
    assert out["reason"] == "gh_not_installed"


def test_merge_pr_success(monkeypatch) -> None:
    monkeypatch.setattr(gp.shutil, "which", lambda _x: "/usr/bin/gh")

    class _P:
        returncode = 0
        stdout = "Merged"
        stderr = ""
    monkeypatch.setattr(gp.subprocess, "run", lambda *a, **k: _P())
    out = gp.merge_pr("https://github.com/o/r/pull/1")
    assert out["merged"] is True


def test_merge_pr_already_merged(monkeypatch) -> None:
    monkeypatch.setattr(gp.shutil, "which", lambda _x: "/usr/bin/gh")

    class _P:
        returncode = 1
        stdout = ""
        stderr = "Pull request already merged"
    monkeypatch.setattr(gp.subprocess, "run", lambda *a, **k: _P())
    out = gp.merge_pr("https://github.com/o/r/pull/1")
    assert out["merged"] is True
    assert out["reason"] == "already_merged"


# ─── is_excluded_path ─────────────────────────────────────────────────


def test_is_excluded_path_artifacts() -> None:
    for p in (
        ".aiforge-worktrees/sub/x.txt",
        "graphify-out/report.html",
        ".aiforge/state.db",
        "node_modules/pkg/index.js",
        "src/__pycache__/m.cpython-311.pyc",
        "build/out.o",
        "dist/app.js",
        ".venv/lib/foo.py",
        "a/b/c.pyc",
        "logs/run.log",
        ".DS_Store",
        ".aiforge-workspace",
        ".env",
        "perf.ndjson",
    ):
        assert gp.is_excluded_path(p) is True, p


def test_is_excluded_path_real_files() -> None:
    for p in (
        "src/main/java/Foo.java",
        "app/db.py",
        "README.md",
        "tests/test_x.py",
        "pyproject.toml",
        # legit source under ambiguously-named dirs — must NOT be excluded
        # (the bare `env`/`build`/`dist`/`target` any-segment match was the bug)
        "myapp/env/settings.py",
        "svc/build/generated.go",
        "pkg/dist/index.ts",
        "module/target/Main.java",
        "config/env/prod.yaml",
    ):
        assert gp.is_excluded_path(p) is False, p


def test_is_excluded_path_toplevel_build_dirs() -> None:
    # build/dist/target/env excluded ONLY at the top level (agrees with the
    # top-level pathspecs). A TOP-LEVEL `env/` is the common virtualenv.
    for p in ("build/out.o", "dist/app.js", "target/classes/A.class",
              "env/lib/python3.12/site-packages/foo.py", "env/bin/activate"):
        assert gp.is_excluded_path(p) is True, p
    # ...but nested env/ is legit source and must NOT be excluded (item 5).
    assert gp.is_excluded_path("myapp/env/settings.py") is False


def test_is_excluded_path_empty() -> None:
    assert gp.is_excluded_path("") is True
    assert gp.is_excluded_path(".") is True


# ─── ensure_artifact_gitignore ────────────────────────────────────────


def test_ensure_artifact_gitignore_creates(tmp_path: Path) -> None:
    added = gp.ensure_artifact_gitignore(str(tmp_path))
    assert ".aiforge-worktrees/" in added
    body = (tmp_path / ".gitignore").read_text()
    for line in (".aiforge/", ".aiforge-worktrees/", ".aiforge-workspace",
                 "graphify-out/", "perf.ndjson"):
        assert line in body


def test_ensure_artifact_gitignore_idempotent(tmp_path: Path) -> None:
    first = gp.ensure_artifact_gitignore(str(tmp_path))
    assert first                       # lines added on first call
    before = (tmp_path / ".gitignore").read_text()
    second = gp.ensure_artifact_gitignore(str(tmp_path))
    assert second == []                # nothing to add the second time
    after = (tmp_path / ".gitignore").read_text()
    assert before == after             # file unchanged (no duplicates)


def test_ensure_artifact_gitignore_appends_preserving_existing(tmp_path: Path) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("*.tmp\nmy_secret\n")
    added = gp.ensure_artifact_gitignore(str(tmp_path))
    body = gi.read_text()
    # user's existing lines preserved
    assert "*.tmp" in body
    assert "my_secret" in body
    # artifact lines appended
    assert "perf.ndjson" in body
    assert "perf.ndjson" in added
    # existing artifact lines aren't duplicated on a re-run
    again = gp.ensure_artifact_gitignore(str(tmp_path))
    assert again == []
    assert body.count("perf.ndjson") == 1
