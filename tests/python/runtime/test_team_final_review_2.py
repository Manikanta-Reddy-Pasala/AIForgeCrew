"""Final-review findings, part 2: resumable runs, read-only rules, the SPEC
review, and the low-severity git hygiene items (see test_team_final_review)."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import team_run_life as life
from aiforge_core.runtime import team_target as tt
from aiforge_core.runtime import team_workspace as tw
from aiforge_core.runtime.parallel_subtasks import _protected as prot
from aiforge_core.runtime.parallel_subtasks._spec_align import (
    align_to_spec,
    keep_planned_subtasks,
    spec_subtasks,
)


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True)


def _repo(path):
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    (path / "money.py").write_text("def fmt(x):\n    return str(x)\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_money.py").write_text("def test_x():\n    pass\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "init")
    return os.path.realpath(str(path))


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    monkeypatch.setitem(life._SWEPT, "done", True)
    yield
    prot._REG.clear()
    tw._RUNS.clear()
    life._PARKED.clear()
    life._CONSENT.clear()
    life._EXCL.clear()


@pytest.fixture
def session_ws(tmp_path):
    ws = tmp_path / "chat-workspaces" / "session-1"
    ws.mkdir(parents=True)
    return str(ws)


# ─── 7: a stopped / questioning run is resumed, not restarted ──────────────


def _turn(monkeypatch, prompt, cwd, history, pipeline, sid=5):
    from aiforge_core.api.routes._chat import _stages
    from aiforge_core.runtime import chat_approve, chat_write_grants
    monkeypatch.setattr(chat_approve, "approvals_required", lambda s: False)
    monkeypatch.setattr(chat_write_grants, "granted", lambda s: [])
    monkeypatch.setattr(_stages, "_pipeline_route", pipeline)
    rd = SimpleNamespace(doc_task=False, route_pipeline=True, notice="")
    rctx = {"done": False}
    hist = list(history) + [{"role": "user", "content": prompt}]
    evs = list(_stages._dispatch_agent_route(
        rd, None, prompt, cwd, sid, hist, lambda t: t, {}, 0.0, False, "",
        rctx))
    return rctx, evs, hist


def test_an_answer_to_a_planner_question_continues_the_same_run(
        tmp_path, session_ws, monkeypatch):
    repo = _repo(tmp_path / "proj")
    seen = []

    def _asks(_pp, prompt, cwd, *a):
        seen.append(cwd)
        with open(os.path.join(cwd, "money.py"), "a") as fh:
            fh.write("# step 1\n")
        _git(cwd, "commit", "-qam", "step 1")
        a[-1]["done"] = True
        yield {"type": "message", "awaiting_input": True,
               "text": "Which currency format?"}
    rctx, _evs, hist = _turn(monkeypatch, f"In {repo} fix money.py",
                             session_ws, [], _asks)
    ws = rctx["team_ws"]
    assert ws.parked and os.path.isdir(ws.cwd) and not ws.closed

    def _answers(_pp, prompt, cwd, *a):
        seen.append(cwd)
        assert "# step 1" in open(os.path.join(cwd, "money.py")).read()
        a[-1]["done"] = True
        yield {"type": "message", "text": "done"}
    rctx2, _evs, _ = _turn(monkeypatch, "euros, two decimals", session_ws,
                           hist, _answers)
    assert rctx2["team_ws"] is ws and seen[0] == seen[1]
    assert ws.closed and not os.path.isdir(ws.cwd)
    assert "# step 1" in _git(repo, "show", f"{ws.branch}:money.py").stdout


def test_continue_after_stop_keeps_the_commits(tmp_path, session_ws,
                                               monkeypatch):
    from aiforge_core.api.routes._chat import _team_route
    repo = _repo(tmp_path / "proj")
    monkeypatch.setattr(_team_route, "_stopped", lambda sid: True)

    def _stopped(_pp, prompt, cwd, *a):
        with open(os.path.join(cwd, "money.py"), "a") as fh:
            fh.write("# half\n")               # left uncommitted at Stop
        a[-1]["done"] = True
        yield {"type": "error", "text": "stopped by user"}
    rctx, _e, hist = _turn(monkeypatch, f"In {repo} fix money.py", session_ws,
                           [], _stopped)
    ws = rctx["team_ws"]
    assert ws.parked
    monkeypatch.setattr(_team_route, "_stopped", lambda sid: False)

    def _goes_on(_pp, prompt, cwd, *a):
        assert "# half" in open(os.path.join(cwd, "money.py")).read()
        a[-1]["done"] = True
        yield {"type": "message", "text": "ok"}
    rctx2, _e, _ = _turn(monkeypatch, "continue", session_ws, hist, _goes_on)
    assert rctx2["team_ws"] is ws and ws.closed


def test_a_new_request_closes_the_parked_run(tmp_path, session_ws,
                                             monkeypatch):
    from aiforge_core.api.routes._chat import _team_route
    repo = _repo(tmp_path / "proj")
    monkeypatch.setattr(_team_route, "_stopped", lambda sid: True)

    def _p(_pp, prompt, cwd, *a):
        a[-1]["done"] = True
        yield {"type": "message", "text": "x"}
    rctx, _e, hist = _turn(monkeypatch, f"In {repo} fix money.py", session_ws,
                           [], _p)
    first = rctx["team_ws"]
    monkeypatch.setattr(_team_route, "_stopped", lambda sid: False)
    rctx2, _e, _ = _turn(monkeypatch, f"In {repo} add a README section",
                         session_ws, hist, _p)
    assert first.closed and rctx2["team_ws"] is not first
    assert not os.path.isdir(first.cwd)


# ─── 8: read-only rules ────────────────────────────────────────────────────


def test_do_not_delete_the_tests_still_lets_a_test_be_added(tmp_path):
    root = str(tmp_path)
    rules = prot.rules_from_texts([
        "Add a test in tests/test_money.py and do not delete the existing tests"])
    prot.register(root, **rules)
    assert not prot.is_protected(root, "tests/test_money.py")
    assert not prot.is_protected(root, "tests/test_other.py")
    assert prot.must_keep(root, "tests/test_other.py")


def test_do_not_rename_is_not_read_only(tmp_path):
    root = str(tmp_path)
    prot.register(root, **prot.rules_from_texts(
        ["we should not rename `auth.py` yet; fix `auth.py`"]))
    assert not prot.is_protected(root, "auth.py")
    assert prot.must_keep(root, "auth.py")


def test_no_is_not_a_prohibition():
    assert prot.from_texts(["no update tests needed, fix money.py"]) == []
    assert prot.from_texts(["casino update the tests"]) == []


def test_the_latest_message_wins(tmp_path):
    root = str(tmp_path)
    texts = ["now update the tests", "ok", "fix money.py; don't edit the tests"]
    prot.register(root, **prot.rules_from_texts(texts))
    assert not prot.is_protected(root, "tests/test_money.py")
    assert prot.from_texts(list(reversed(texts))) == [prot.TESTS]


def test_a_deleted_keep_file_is_restored_but_an_edit_is_kept(tmp_path):
    repo = _repo(tmp_path / "r")
    prot.register(repo, **prot.rules_from_texts(
        ["fix it and do not delete the existing tests"]))
    with open(os.path.join(repo, "money.py"), "a") as fh:
        fh.write("# x\n")
    os.remove(os.path.join(repo, "tests", "test_money.py"))
    assert prot.revert(repo, "HEAD") == ["tests/test_money.py"]
    assert os.path.isfile(os.path.join(repo, "tests", "test_money.py"))
    assert "# x" in open(os.path.join(repo, "money.py")).read()


def test_do_not_edit_the_tests_still_protects(tmp_path):
    root = str(tmp_path)
    prot.register(root, **prot.rules_from_texts(
        ["In /x fix money.py so every test in tests/ passes. Run pytest there "
         "to check. Do not edit the tests."]))
    assert prot.is_protected(root, "tests/test_money.py")
    assert not prot.is_protected(root, "money.py")


# ─── 9: a truncated SPEC review keeps the planned subtasks ────────────────


_SPEC = ("# Project Spec\n\n## Goal\nfix\n\n## Subtasks (3)\n\n"
         "1. **money** — fix `money.py`.\n2. **fmt** — add `fmt.py`.\n"
         "3. **cli** — add `cli.py`.\n\n## Acceptance\n- tests pass\n")


def test_a_review_cut_mid_subtasks_keeps_the_original_list():
    cut = ("# Project Spec\n\n## Goal\nfix, clearer\n\n## Subtasks (3)\n\n"
           "1. **money** — fix `money.py`.\n2. **fm")
    out, restored = keep_planned_subtasks(_SPEC, cut)
    assert restored and [i["slug"] for i in spec_subtasks(out)] == [
        "money", "fmt", "cli"]
    assert "fix, clearer" in out and "## Acceptance" in out
    subs = [{"slug": s, "path": f"{s}.py", "goal": s}
            for s in ("money", "fmt", "cli")]
    assert len(align_to_spec(subs, out)[0]) == 3


def test_a_complete_narrowing_review_is_kept():
    narrowed = _SPEC.replace("2. **fmt** — add `fmt.py`.\n", "")
    out, restored = keep_planned_subtasks(_SPEC, narrowed)
    assert not restored and out == narrowed


def test_review_spec_sends_the_whole_spec_and_merges_a_cut_reply(monkeypatch):
    from aiforge_core.runtime import review_gates as rg
    big = _SPEC.replace("## Goal\nfix", "## Goal\n" + "detail line\n" * 800)
    sent = {}

    def _once(prompt, max_tokens, fast=False):
        sent.update(prompt=prompt, max_tokens=max_tokens)
        return big[:big.index("2. **fmt**")] + "2. **fm"
    monkeypatch.setattr(rg, "review_once", _once)
    out, note = rg.review_spec("req", big)
    assert big in sent["prompt"] and len(big) > 6000
    assert sent["max_tokens"] > 3072
    assert [i["slug"] for i in spec_subtasks(out)] == ["money", "fmt", "cli"]
    assert "cut short" in note


# ─── LOW ───────────────────────────────────────────────────────────────────


def test_the_shared_exclude_is_restored_on_close(tmp_path):
    repo = _repo(tmp_path / "r")
    excl = os.path.join(repo, ".git", "info", "exclude")
    os.makedirs(os.path.dirname(excl), exist_ok=True)
    with open(excl, "w") as fh:
        fh.write("mine.txt\n")
    ws = tw.open_run(repo, "fix")
    assert ".aiforge-baseline" in open(excl).read()
    list(tw.close(ws))
    assert open(excl).read() == "mine.txt\n"


def test_no_identity_goes_per_call_not_into_os_environ(tmp_path, monkeypatch):
    repo = _repo(tmp_path / "r")
    real_out = tw._out
    monkeypatch.setattr(tw, "_out", lambda args, cwd: "" if args[:2] == [
        "var", "GIT_COMMITTER_IDENT"] else real_out(args, cwd))
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME", "GIT_AUTHOR_EMAIL",
              "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(k, raising=False)
    before = dict(os.environ)
    ws = tw.open_run(repo, "fix")
    assert dict(os.environ) == before
    env = tw.git_env(os.path.join(ws.cwd, "money.py"))
    assert env["GIT_COMMITTER_EMAIL"] == "aiforge@local"
    assert tw.git_env(str(tmp_path)) is None
    list(tw.close(ws))


def test_dirty_overlap_keeps_dot_folders(tmp_path):
    from aiforge_core.runtime.parallel_subtasks._stream_guard import dirty_overlap_stop
    repo = _repo(tmp_path / "r")
    ws = tw.open_run(repo, "fix")
    ws.dirty = [".github/workflows/ci.yml"]
    evs = list(dirty_overlap_stop(ws.cwd, [{"path": ".github/workflows/ci.yml"}]))
    assert evs and ".github/workflows/ci.yml" in evs[0]["text"]
    ws.dirty = ["github/workflows/ci.yml"]
    assert list(dirty_overlap_stop(ws.cwd, [{"path": ".github/workflows/ci.yml"}])) == []
    list(tw.close(ws))


def test_close_puts_read_only_files_back_to_the_start_not_head(tmp_path):
    repo = _repo(tmp_path / "r")
    ws = tw.open_run(repo, "fix")
    prot.register(ws.cwd, [prot.TESTS])
    with open(os.path.join(ws.cwd, "tests", "test_money.py"), "a") as fh:
        fh.write("# weakened\n")
    _git(ws.cwd, "commit", "-qam", "a writer committed it")
    list(tw.close(ws))
    assert "# weakened" not in _git(
        repo, "show", f"{ws.branch}:tests/test_money.py").stdout


def test_submodules_are_initialised_in_the_run_worktree(tmp_path, monkeypatch):
    for k, v in (("GIT_CONFIG_COUNT", "1"),
                 ("GIT_CONFIG_KEY_0", "protocol.file.allow"),
                 ("GIT_CONFIG_VALUE_0", "always")):
        monkeypatch.setenv(k, v)
    lib = _repo(tmp_path / "lib")
    repo = _repo(tmp_path / "r")
    _git(repo, "submodule", "add", "-q", lib, "lib")
    _git(repo, "commit", "-qm", "sub")
    ws = tw.open_run(repo, "fix")
    assert os.path.isfile(os.path.join(ws.cwd, "lib", "money.py"))
    list(tw.close(ws))


def test_a_branch_named_aiforge_does_not_block_the_run(tmp_path):
    repo = _repo(tmp_path / "r")
    _git(repo, "branch", "aiforge")
    ws = tw.open_run(repo, "fix money.py")
    assert ws.branch.startswith("aiforge-run/")
    list(tw.close(ws))


def test_an_indented_bullet_is_an_instruction_not_a_paste(tmp_path,
                                                          session_ws):
    repo = _repo(tmp_path / "proj")
    t = tt.resolve_team_target([f"Tasks:\n    - fix money.py in {repo}\n"],
                               session_ws)
    assert t.retargeted and t.cwd == repo
    code = tt.resolve_team_target([f"see:\n    open('{repo}/x')\n"], session_ws)
    assert not code.retargeted
