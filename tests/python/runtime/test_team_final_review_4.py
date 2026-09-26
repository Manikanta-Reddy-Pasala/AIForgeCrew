"""Final-review findings, round 3 (helpers as in _3): a refused commit must never lose work,
a parked run resumes only on a continue/answer, idioms are not prohibitions,
bold/backtick subtask forms, shell commands naming the real repo in a team
run, request-framed "apply", and the shared exclude with overlapping runs."""
from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from aiforge_core.runtime import team_run_life as life
from aiforge_core.runtime import team_workspace as tw
from aiforge_core.runtime.parallel_subtasks import _protected as prot


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


def _branches(repo):
    return _git(repo, "branch", "--format=%(refname:short)").stdout.split()


def _turn(monkeypatch, prompt, cwd, history, pipeline, sid=5):
    from aiforge_core.api.routes._chat import _stages
    from aiforge_core.runtime import chat_approve, chat_write_grants
    monkeypatch.setattr(chat_approve, "approvals_required", lambda s: False)
    monkeypatch.setattr(chat_write_grants, "granted", lambda s: [])
    monkeypatch.setattr(_stages, "_pipeline_route", pipeline)
    rd = SimpleNamespace(doc_task=False, route_pipeline=True, notice="")
    rctx = {"done": False}
    hist = list(history) + [{"role": "user", "content": prompt}]
    list(_stages._dispatch_agent_route(rd, None, prompt, cwd, sid, hist,
                                       lambda t: t, {}, 0.0, False, "", rctx))
    return rctx, hist


def _asks(_pp, prompt, cwd, *a):
    a[-1]["done"] = True
    yield {"type": "message", "awaiting_input": True, "text": "Which database?"}


def _commits(_pp, prompt, cwd, *a):
    with open(os.path.join(cwd, "money.py"), "a") as fh:
        fh.write("# done\n")
    _git(cwd, "commit", "-qam", "done")
    a[-1]["done"] = True
    yield {"type": "message", "text": "ok"}


# ─── 1: an answer keeps the earlier "merge it into main" ───────────────────


def test_an_answer_keeps_the_requested_merge(tmp_path, session_ws,
                                             monkeypatch):
    repo = _repo(tmp_path / "proj")
    rctx, hist = _turn(monkeypatch, f"In {repo} fix money.py and merge it "
                       "into main", session_ws, [], _asks)
    ws = rctx["team_ws"]
    assert ws.apply and ws.parked
    rctx2, _ = _turn(monkeypatch, "use postgres", session_ws, hist, _commits)
    assert rctx2["team_ws"] is ws and ws.apply
    assert "# done" in (tmp_path / "proj" / "money.py").read_text()


def test_an_answer_can_cancel_the_merge(tmp_path, session_ws, monkeypatch):
    repo = _repo(tmp_path / "proj")
    rctx, hist = _turn(monkeypatch, f"In {repo} fix money.py and merge it "
                       "into main", session_ws, [], _asks)
    rctx2, _ = _turn(monkeypatch, "postgres, and don't merge it yet",
                     session_ws, hist, _commits)
    assert rctx2["team_ws"] is rctx["team_ws"] and not rctx2["team_ws"].apply
    assert "# done" not in (tmp_path / "proj" / "money.py").read_text()


@pytest.mark.parametrize("text,no", [
    ("don't merge it", True), ("do not fast-forward", True),
    ("leave main alone", True), ("keep it on its own branch", True),
    ("use postgres", False), ('the doc says "don\'t merge"', False),
])
def test_refuses_apply(text, no):
    assert tw.refuses_apply(text) is no


# ─── 2: pending work is judged by what seal would commit ───────────────────


def test_ignored_paths_are_not_pending_work(tmp_path):
    repo = tmp_path / "r"
    _repo(repo)
    (repo / ".vscode").mkdir()
    (repo / ".vscode" / "settings.json").write_text("{}\n")
    _git(repo, "add", "-f", ".vscode/settings.json")
    _git(repo, "commit", "-qm", "vscode")
    ws = tw.open_run(str(repo), "fix", apply=True)
    with open(os.path.join(ws.cwd, ".vscode", "settings.json"), "a") as fh:
        fh.write("// local\n")
    with open(os.path.join(ws.cwd, ".env"), "w") as fh:
        fh.write("SECRET=1\n")
    with open(os.path.join(ws.cwd, "money.py"), "a") as fh:
        fh.write("# fixed\n")
    tw.seal(ws.cwd)
    assert not life.has_pending(ws.cwd)
    notes = list(tw.close(ws))
    assert not os.path.isdir(ws.cwd)
    assert "fast-forward" in notes[-1]["text"]
    assert "# fixed" in (repo / "money.py").read_text()


# ─── 5: only a plausible answer resumes a question-parked run ─────────────


@pytest.mark.parametrize("text,ok", [
    ("use postgres", True), ("euros, two decimals", True), ("yes", True),
    ("continue", True),
    ("fix the login bug", False), ("can you add dark mode?", False),
    ("please add a README", False), ("I want you to build a CLI", False),
    ("yes, but build X instead", False), ("yes, and can you add tests", False),
])
def test_what_answers_a_pending_question(text, ok):
    assert life.continues(text, "question") is ok


def test_a_request_after_a_question_closes_the_parked_run(tmp_path,
                                                          session_ws,
                                                          monkeypatch):
    repo = _repo(tmp_path / "proj")
    rctx, hist = _turn(monkeypatch, f"In {repo} fix money.py", session_ws, [],
                       _asks)
    first = rctx["team_ws"]
    rctx2, _ = _turn(monkeypatch, "can you add dark mode?", session_ws, hist,
                     _commits)
    assert first.closed and rctx2["team_ws"] is not first


# ─── 6: whitelisted words between negation and verb still protect ─────────


@pytest.mark.parametrize("text", [
    "never directly edit the tests", "please do not try to modify tests",
    "don't ever touch the tests", "do not manually change the tests",
])
def test_whitelisted_adverbs_still_protect(text):
    assert prot.from_texts([text]) == [prot.TESTS]


@pytest.mark.parametrize("text", [
    "don't forget to update the tests", "don't just change the tests",
    "don't hesitate to modify the tests", "do not only edit the tests",
])
def test_idioms_stay_permissions(text):
    assert prot.from_texts([text]) == []
