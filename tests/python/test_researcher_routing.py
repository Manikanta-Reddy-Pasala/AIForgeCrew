"""Tests for ``aiforge_core.runtime.researcher_routing``.

Drives :func:`should_skip_researcher` against synthetic title/body
fixtures + a tmp-path git repo so the git-log probe runs under
realistic conditions (no monkey-patching of subprocess).
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from aiforge_core.runtime.researcher_routing import (
    should_skip_researcher,
)


# ─── helpers ───────────────────────────────────────────────────────────


def _init_repo(path: Path) -> None:
    """Create a minimal git repo with one initial commit. Tests can
    add more commits to drive the keyword probe."""
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
    )
    (path / "README.md").write_text("test repo\n")
    subprocess.run(["git", "-C", str(path), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "initial commit"],
        check=True, env={**os.environ, "GIT_AUTHOR_NAME": "Test",
                         "GIT_AUTHOR_EMAIL": "t@t",
                         "GIT_COMMITTER_NAME": "Test",
                         "GIT_COMMITTER_EMAIL": "t@t"},
    )


def _add_commit(path: Path, message: str) -> None:
    """Append a commit with ``message`` (so the keyword probe finds it)."""
    f = path / f"f-{len(message)}.txt"
    f.write_text(message)
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", message],
        check=True, env={**os.environ, "GIT_AUTHOR_NAME": "Test",
                         "GIT_AUTHOR_EMAIL": "t@t",
                         "GIT_COMMITTER_NAME": "Test",
                         "GIT_COMMITTER_EMAIL": "t@t"},
    )


# ─── reference-pattern detection ──────────────────────────────────────


def test_reference_word_blocks_skip(tmp_path):
    """A body that mentions ``existing`` MUST keep the Researcher
    running even if the git log is empty."""
    _init_repo(tmp_path)
    skip, reason = should_skip_researcher(
        title="Add feature X",
        body="See the existing implementation in foo.py for reference.",
        repo_root=str(tmp_path),
    )
    assert skip is False
    assert reason == "has_reference_word"


@pytest.mark.parametrize("phrase", [
    "We have previously shipped this in v1.",
    "Refer to foo.py for the pattern.",
    "Reuse the existing helper in bar.py.",
    "Build it as in foo's setup.",
    "Make it similar to baz.",
    "See the above-mentioned helper.",
    "Behave like the existing handler.",
])
def test_each_reference_phrase_blocks(phrase, tmp_path):
    _init_repo(tmp_path)
    skip, reason = should_skip_researcher(
        title="Add foo", body=phrase,
        repo_root=str(tmp_path),
    )
    assert skip is False
    assert reason == "has_reference_word"


def test_no_reference_words_in_clean_greenfield_body(tmp_path):
    """Pure greenfield body (no reference patterns) + empty repo
    history means we DO skip."""
    _init_repo(tmp_path)
    skip, reason = should_skip_researcher(
        title="Bootstrap zoozle service",
        body="Build a brand new FastAPI scaffold from scratch.",
        repo_root=str(tmp_path),
    )
    assert skip is True
    assert reason == "greenfield"


# ─── git-log probe ─────────────────────────────────────────────────────


def test_git_log_match_blocks_skip(tmp_path):
    """When a prior commit subject matches the title's keyword, the
    Researcher SHOULD run — there's actual prior work to surface."""
    _init_repo(tmp_path)
    _add_commit(tmp_path, "feat(zoozle): scaffold")
    skip, reason = should_skip_researcher(
        title="Bootstrap zoozle service",
        body="No reference words here.",
        repo_root=str(tmp_path),
    )
    assert skip is False
    assert reason == "git_log_match"


def test_git_log_case_insensitive_match(tmp_path):
    """Probe is ``-i`` — uppercase title shouldn't miss lowercase
    commits."""
    _init_repo(tmp_path)
    _add_commit(tmp_path, "fix: ZOOZLE handler crash")
    skip, reason = should_skip_researcher(
        title="Add zoozle metrics",
        body="No reference words here.",
        repo_root=str(tmp_path),
    )
    assert skip is False
    assert reason == "git_log_match"


def test_missing_repo_does_not_crash():
    """Path with no .git directory — caller might be running outside
    a workspace. Conservative behaviour: do NOT skip (the helper
    returns ``True`` for the git probe so it acts as if matched)."""
    skip, reason = should_skip_researcher(
        title="Anything", body="No refs",
        repo_root="/tmp/this-path-definitely-does-not-have-a-git-repo-x9z",
    )
    # Conservative — don't skip. Returns ``git_log_match`` because the
    # probe defaults to True on missing repo (caller logs the reason).
    assert skip is False


# ─── env override ─────────────────────────────────────────────────────


def test_force_env_blocks_skip(tmp_path, monkeypatch):
    """``AIFORGE_RESEARCHER_FORCE=1`` must force-on Researcher even
    on a perfectly greenfield ticket."""
    _init_repo(tmp_path)
    monkeypatch.setenv("AIFORGE_RESEARCHER_FORCE", "1")
    skip, reason = should_skip_researcher(
        title="Bootstrap quaxic service",
        body="No reference words. Brand new code.",
        repo_root=str(tmp_path),
    )
    assert skip is False
    assert reason == "forced_on"


def test_force_env_zero_does_not_force(tmp_path, monkeypatch):
    """``AIFORGE_RESEARCHER_FORCE=0`` is the default behaviour — must
    NOT pin the Researcher on (regression guard)."""
    _init_repo(tmp_path)
    monkeypatch.setenv("AIFORGE_RESEARCHER_FORCE", "0")
    skip, reason = should_skip_researcher(
        title="Bootstrap quaxic service",
        body="No reference words. Brand new code.",
        repo_root=str(tmp_path),
    )
    assert skip is True
    assert reason == "greenfield"


# ─── stopword filtering ───────────────────────────────────────────────


def test_title_with_only_stopwords_falls_through_to_greenfield(tmp_path):
    """Title made entirely of stopwords (``add``, ``to``, ``the``)
    yields no usable keyword. Combined with no reference words and an
    empty repo, that means skip=True."""
    _init_repo(tmp_path)
    skip, reason = should_skip_researcher(
        title="Add to the build", body="Brand new code.",
        repo_root=str(tmp_path),
    )
    # No keyword survives the stopword filter, so the git probe is
    # skipped entirely. Result: greenfield.
    assert skip is True
    assert reason == "greenfield"
