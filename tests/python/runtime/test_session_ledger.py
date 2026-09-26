"""Session execution ledger — no-repeat context + working-workflow capture."""
from __future__ import annotations

import pytest

from aiforge_core.runtime import session_ledger as sl


def _msgs(monkeypatch, messages):
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_messages", lambda sid: messages)


def test_ledger_dedupes_and_tracks_outcome(monkeypatch):
    _msgs(monkeypatch, [
        {"role": "assistant", "steps": [
            {"type": "tool", "name": "run_command", "args": {"cmd": "pytest"},
             "result": {"ok": False}},
            {"type": "tool", "name": "file_write", "args": {"path": "a.py"},
             "result": {"ok": True}},
        ]},
        {"role": "assistant", "steps": [
            {"type": "tool", "name": "run_command", "args": {"cmd": "pytest"},
             "result": {"ok": True}},          # retry succeeded → outcome flips
            {"type": "tool", "name": "grep", "args": {"q": "x"},
             "result": {"ok": True}},          # read-only → not in ledger
        ]},
    ])
    items = sl.ledger_items(1)
    keys = [i["key"] for i in items]
    assert keys == ["cmd:pytest", "write:a.py"]      # deduped, order preserved
    assert items[0]["outcome"] is True               # latest outcome wins
    blk = sl.ledger_block(1)
    assert "ALREADY EXECUTED" in blk
    assert "✅ ran `pytest`" in blk
    assert "wrote `a.py`" in blk


def test_ledger_empty_when_no_tools(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "thought", "text": "hmm"}]}])
    assert sl.ledger_block(1) == ""


def _stub_verify(monkeypatch, result):
    """Patch the LLM verification to return `result` (dict) or raise (→ None)."""
    from types import SimpleNamespace as NS

    def fake(role, messages, model, **k):
        if result is None:
            raise RuntimeError("no model")
        return NS(model_dump=lambda: result)
    monkeypatch.setattr("aiforge_core.llm.structured.structured_complete", fake)


def test_capture_working_workflow_verified(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command", "args": {"cmd": "npm ci"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "npm test"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "flaky"},
         "result": {"ok": False}},             # failed → NOT even considered
    ]}])
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_session", lambda sid: {"title": "Set up CI"})
    _stub_verify(monkeypatch, {"is_reusable": True, "name": "ci-setup",
                               "description": "install + test",
                               "steps": ["npm ci", "npm test"], "triggers": ["ci"]})
    seen = {}
    from aiforge_core.runtime import workflows
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda name, description, body, **k: seen.update(
                            name=name, body=body, triggers=k.get("triggers")) or {"ok": True, "name": name})
    from aiforge_core.memory import md_store
    caps = []
    monkeypatch.setattr(md_store, "capture",
                        lambda kind, text, **k: caps.append((kind, k.get("tags"))) or {})
    r = sl.capture_working_workflow(7, repo="myrepo")
    assert r["ok"]
    assert seen["name"] == "session-ci-setup"       # LLM-refined name
    assert seen["triggers"] == ["ci"]
    assert "`npm ci`" in seen["body"]
    assert "`npm test`" in seen["body"]
    assert "flaky" not in seen["body"]
    assert caps
    assert "repo:myrepo" in caps[0][1]
    assert "workflow" in caps[0][1]


def test_capture_skips_when_llm_says_not_reusable(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command", "args": {"cmd": "ls"}, "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "cat x"}, "result": {"ok": True}},
    ]}])
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_session", lambda sid: {"title": "poking around"})
    _stub_verify(monkeypatch, {"is_reusable": False})
    from aiforge_core.runtime import workflows
    called = {"n": 0}
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or {"ok": True})
    r = sl.capture_working_workflow(9, repo="r")
    assert r.get("skipped") == "not_reusable"
    assert called["n"] == 0                          # nothing written


def test_capture_falls_back_when_no_model(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command", "args": {"cmd": "make build"}, "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "make deploy"}, "result": {"ok": True}},
    ]}])
    from aiforge_core.runtime import chat_store
    monkeypatch.setattr(chat_store, "get_session", lambda sid: {"title": "Release"})
    _stub_verify(monkeypatch, None)                  # no model → raise → fallback
    seen = {}
    from aiforge_core.runtime import workflows
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda name, description, body, **k: seen.update(body=body, name=name) or {"ok": True})
    from aiforge_core.memory import md_store
    monkeypatch.setattr(md_store, "capture", lambda *a, **k: {})
    r = sl.capture_working_workflow(3, repo="r")
    assert r["ok"]
    assert "make build" in seen["body"]
    assert "unverified" in seen["body"]


def _capture_notes(monkeypatch):
    seen = []

    def write(text, **k):
        seen.append((text, k))
        return {"ok": True, "id": len(seen)}

    monkeypatch.setattr(
        "aiforge_core.runtime.tools.memory_write.memory_write", write)
    return seen


def test_a_working_ssh_is_stored_for_the_next_session(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "ssh nuc 'bash -lc \"pytest -q\"'"},
         "result": {"ok": True}}]}])
    seen = _capture_notes(monkeypatch)
    out = sl.remember_working_ops(11, repo="shop")
    assert out["written"] == 1
    text, kwargs = seen[0]
    assert "ssh nuc" in text and "bash -lc" in text
    assert kwargs["scope"] == "global"
    assert "tool:ssh" in kwargs["tags"]


def test_a_failed_ssh_and_a_secret_are_not_stored(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "ssh nuc 'ls'"},
         "result": {"ok": False}},
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "ssh ms 'echo token=abc'"},
         "result": {"ok": True}},
    ]}])
    seen = _capture_notes(monkeypatch)
    sl.remember_working_ops(12, repo="shop")
    assert seen == []


def test_the_stored_ssh_keeps_the_user_and_the_port(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "ssh -p 2222 deploy@nuc 'bash -lc \"uptime\"'"},
         "result": {"ok": True}}]}])
    seen = _capture_notes(monkeypatch)
    sl.remember_working_ops(14, repo="shop")
    assert "ssh -p 2222 deploy@nuc" in seen[0][0]


def test_ssh_keygen_and_a_credential_are_not_a_connection(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "ssh-keygen -t ed25519"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "git clone https://oauth2:glpat-abc@gitlab.com/x.git"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "pytest -q 2>&1 | tail"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "GITLAB_TOKEN=glx9abc python3 deploy.py"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "sshpass -p hunter2 ssh nuc git pull"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "npm start & sleep 5"},
         "result": {"ok": True}},
    ]}])
    seen = _capture_notes(monkeypatch)
    sl.remember_working_ops(15, repo="shop")
    assert seen == []


def test_a_project_command_that_worked_is_stored_on_the_repo(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "pytest -q tests/python/runtime/test_chat_router.py"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command", "args": {"cmd": "ls"},
         "result": {"ok": True}},
    ]}])
    seen = _capture_notes(monkeypatch)
    sl.remember_working_ops(13, repo="AIForgeCrew")
    assert len(seen) == 1
    assert seen[0][0].startswith("In AIForgeCrew, this command succeeded: pytest")
    assert seen[0][1]["repo"] == "AIForgeCrew"
    assert seen[0][1]["scope"] == ""


def test_capture_skips_too_few_working(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command", "args": {"cmd": "ls"},
         "result": {"ok": True}}]}])
    assert sl.capture_working_workflow(1, repo="r")["skipped"] == "too_few_working_steps"


@pytest.mark.parametrize("cmd", [
    "curl -u admin:hunter2 https://api.example.com/x",
    "curl --user admin:hunter2 https://api.example.com/x",
    "curl --user=admin:hunter2 https://api.example.com/x",
    "mysql -uroot -phunter2 shop",
    "mysql -u root -p hunter2 shop",
    "psql --password=hunter2 -h db shop",
    "some-cli --password hunter2",
    "curl -H 'Authorization: Bearer abc123' https://api.example.com",
    "curl -H 'Authorization:Basic YWRtaW46aHVudGVy' https://api.example.com",
    "curl -H 'X-Api: Bearer abc123' https://api.example.com",
    "curl https://api.example.com/x?token=abc123",
    "python3 deploy.py --auth=abc123",
    "git clone https://deploy:hunter2@git.example.com/x.git",
    "rsync admin:hunter2@backup.example.com::share /tmp/x",
    "aws s3 ls --profile AKIAABCDEFGHIJKLMNOP",
    "curl -H 'x: eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0' https://api.example.com",
])
def test_a_command_carrying_a_credential_is_never_stored(cmd):
    assert sl._SECRET_RE.search(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "ssh -p 2222 deploy@nuc uptime",
    "pytest -q tests/python",
    "docker run -u 1000:1000 alpine true",
    "git clone git@github.com:org/repo.git",
    "scp build.tar deploy@nuc:/tmp/",
    "npm run build",
])
def test_an_ordinary_command_is_not_mistaken_for_a_credential(cmd):
    assert not sl._SECRET_RE.search(cmd), cmd


def test_the_ssh_note_says_what_actually_worked(monkeypatch):
    """A command that ran without a login shell is not taught as bash -lc."""
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "ssh ms 'uptime'"},
         "result": {"ok": True}}]}])
    seen = _capture_notes(monkeypatch)
    sl.remember_working_ops(16, repo="shop")
    text = seen[0][0]
    assert "ssh ms '<command>'" in text
    assert "bash -lc" not in text


def test_a_login_shell_that_worked_is_kept_in_the_note(monkeypatch):
    _msgs(monkeypatch, [{"role": "assistant", "steps": [
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "ssh ms 'uptime'"},
         "result": {"ok": True}},
        {"type": "tool", "name": "run_command",
         "args": {"cmd": "ssh ms 'zsh -lc \"pytest -q\"'"},
         "result": {"ok": True}}]}])
    seen = _capture_notes(monkeypatch)
    sl.remember_working_ops(17, repo="shop")
    assert len(seen) == 1
    assert "zsh -lc" in seen[0][0]
