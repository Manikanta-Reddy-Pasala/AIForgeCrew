"""Two chats in one repository — a worktree each, on its own branch."""

from __future__ import annotations

import pytest
from aiforge_cli import worktrees as wt

PORCELAIN = """worktree /home/ai/work/repo
HEAD 1f0e3dad99908345f7439f8ffabdffc418elab6d
branch refs/heads/main

worktree /home/ai/work/repo/.worktrees/fix-retry
HEAD 8f14e45fceea167a5a36dedd4bea2543aaaaaaaa
branch refs/heads/wt/fix-retry

worktree /home/ai/work/repo/.worktrees/spike
HEAD c9f0f895fb98ab9159f51fd0297e236dbbbbbbbb
detached
"""


def test_the_porcelain_listing_is_parsed_not_scraped():
    trees = wt.parse_list(PORCELAIN)
    assert [t.name for t in trees] == ["repo", "fix-retry", "spike"]
    assert trees[1].branch == "wt/fix-retry"
    assert trees[2].branch == "(detached)"      # no branch line at all
    assert trees[0].head == "1f0e3dad9990"      # short, like git shows it


def test_a_worktree_lives_inside_the_repo_so_it_needs_no_new_mount():
    from aiforge_cli import paths
    repo = "/home/ai/work/repo"
    path = wt.worktree_path(repo, "fix-retry")
    assert path == f"{repo}/.worktrees/fix-retry"
    # Inside the repo's own mount: no mount change, so no container restart and
    # nobody else on the machine is interrupted.
    assert paths.covering_mount(path, [repo], platform="posix") == repo


def test_a_name_that_cannot_be_a_branch_is_refused():
    assert wt.safe_name("fix-retry") is None
    assert "refname" in wt.safe_name("fix retry")
    assert "refname" in wt.safe_name("feature/x")
    assert wt.safe_name("") is not None
    assert wt.safe_name(".hidden") is not None


class FakeRunner:
    """Records argv and answers from a script keyed by the git subcommand."""

    def __init__(self, answers: dict[str, tuple[int, str]]):
        self.answers = answers
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> tuple[int, str]:
        self.calls.append(cmd)
        for key, answer in self.answers.items():
            if key in " ".join(cmd):
                return answer
        return (1, "fatal: not scripted")


def test_git_runs_inside_the_box_so_the_host_needs_no_git():
    runner = FakeRunner({"worktree list": (0, PORCELAIN)})
    git = wt.Git(runner=runner, container="aiforge", as_user="1000:1000")
    git.list("/home/ai/work/repo")
    argv = runner.calls[0]
    assert argv[:2] == ["docker", "exec"]
    assert "aiforge" in argv
    assert "git" in argv


def test_the_box_acts_as_the_host_user_not_root():
    # docker exec defaults to the image user (root); a worktree created that
    # way leaves root-owned refs in .git and the repo's owner cannot remove
    # the branch or the folder afterwards.
    runner = FakeRunner({"worktree list": (0, PORCELAIN)})
    git = wt.Git(runner=runner, as_user="1000:1000")
    git.list("/repo")
    argv = runner.calls[0]
    assert "-u" in argv
    assert argv[argv.index("-u") + 1] == "1000:1000"


def test_the_host_git_is_only_a_fallback():
    # Box first (it is guaranteed to have git and to see the repo at the same
    # path); the host only when the box cannot answer.
    runner = FakeRunner({"docker exec": (1, "Error: No such container: aiforge"),
                         "git -C": (0, PORCELAIN)})
    git = wt.Git(runner=runner)
    assert len(git.list("/repo")) == 3
    assert runner.calls[0][0] == "docker"
    assert runner.calls[1][0] == "git"


def test_git_failing_everywhere_reports_gits_own_words():
    runner = FakeRunner({"": (1, "hint: ignore me\nfatal: not a git repository")})
    git = wt.Git(runner=runner)
    with pytest.raises(wt.GitError) as exc:
        git.list("/tmp")
    assert str(exc.value) == "fatal: not a git repository"   # the hint is dropped


def test_adding_an_existing_worktree_is_not_an_error():
    runner = FakeRunner({"worktree list": (0, PORCELAIN)})
    git = wt.Git(runner=runner)
    tree = git.add("/home/ai/work/repo", "fix-retry")
    assert tree.branch == "wt/fix-retry"
    # Running `aiforge worktree add fix-retry` twice must land in the same
    # place, not fail on "branch already exists".
    assert not any("worktree add" in " ".join(c) for c in runner.calls)


NEW_TREE = """worktree /home/ai/work/repo/.worktrees/new-thing
HEAD aaaa111122223333444455556666777788889999
branch refs/heads/wt/new-thing
"""

OLD_TREE = """worktree /home/ai/work/repo/.worktrees/old
HEAD bbbb111122223333444455556666777788889999
branch refs/heads/wt/old
"""


class StatefulRunner:
    """git, before and after the add. The first listing must NOT contain the
    new tree, or `add` rightly concludes it is already there."""

    def __init__(self, *, branch_exists: bool, after: str):
        self.calls: list[list[str]] = []
        self.branch_exists = branch_exists
        self.after = after
        self.added = False

    def __call__(self, cmd):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if "show-ref" in joined:
            return (0 if self.branch_exists else 1, "")
        if "worktree add" in joined:
            self.added = True
            return (0, "")
        if "worktree list" in joined:
            return (0, PORCELAIN + "\n" + self.after if self.added else PORCELAIN)
        return (0, "")


def test_a_new_worktree_gets_its_own_branch():
    runner = StatefulRunner(branch_exists=False, after=NEW_TREE)
    calls = runner.calls
    git = wt.Git(runner=runner)
    tree = git.add("/home/ai/work/repo", "new-thing")
    assert tree.branch == "wt/new-thing"
    add = next(c for c in calls if "worktree add" in " ".join(c))
    assert "-b" in add
    assert "wt/new-thing" in add


def test_an_existing_branch_is_checked_out_rather_than_recreated():
    runner = StatefulRunner(branch_exists=True, after=OLD_TREE)
    calls = runner.calls
    git = wt.Git(runner=runner)
    git.add("/home/ai/work/repo", "old")
    add = next(c for c in calls if "worktree add" in " ".join(c))
    assert "-b" not in add
