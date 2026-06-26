"""Deterministic Rule / Memory / Feedback capture (rule_capture.py).

Covers: classify strict-JSON parse + fail-open fallbacks; store routing per
category × scope incl. session isolation + project .aiforge/rules write;
apply_behavioral commit/delete flag setting + flag_active precedence; the
chat inline gate honoring commit_auto_approve (the operator's example); and
fail-open when classify raises.
"""
import importlib
import json

import pytest


@pytest.fixture
def rc(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.delenv("AIFORGE_RULE_CAPTURE_DISABLE", raising=False)
    monkeypatch.delenv("AIFORGE_RULE_CAPTURE_MIN_CONFIDENCE", raising=False)
    from aiforge_core.memory import md_store
    importlib.reload(md_store)
    from aiforge_core.runtime import rule_capture
    importlib.reload(rule_capture)
    return rule_capture


def _mock_llm(rc, payload):
    """Patch the single LLM call to return ``payload`` (str or dict)."""
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    rc._llm_complete = lambda *a, **k: raw  # type: ignore


# ─────────────────────────── classify ───────────────────────────────

def test_classify_strict_json(rc):
    _mock_llm(rc, {"category": "rule", "scope": "global",
                   "canonical": "always use yarn", "confidence": 0.9,
                   "task_present": False})
    c = rc.classify("from now on always use yarn")
    assert c["category"] == "rule" and c["scope"] == "global"
    assert c["canonical"] == "always use yarn"
    assert c["task_present"] is False


def test_classify_json_with_surrounding_prose(rc):
    _mock_llm(rc, 'Sure! Here:\n{"category":"memory","scope":"project",'
                  '"canonical":"db at db.staging","confidence":0.8}\nthanks')
    c = rc.classify("the staging db is db.staging")
    assert c["category"] == "memory" and c["scope"] == "project"


def test_classify_bad_json_falls_open_to_none(rc):
    _mock_llm(rc, "not json at all <thinking> blah")
    assert rc.classify("hi")["category"] == "none"


def test_classify_llm_error_falls_open(rc):
    def boom(*a, **k):
        raise RuntimeError("model down")
    rc._llm_complete = boom  # type: ignore
    assert rc.classify("anything")["category"] == "none"


def test_classify_low_confidence_is_none(rc):
    _mock_llm(rc, {"category": "rule", "scope": "global",
                   "canonical": "maybe", "confidence": 0.3})
    assert rc.classify("eh")["category"] == "none"


def test_classify_disable_env(rc, monkeypatch):
    monkeypatch.setenv("AIFORGE_RULE_CAPTURE_DISABLE", "1")
    _mock_llm(rc, {"category": "rule", "scope": "global",
                   "canonical": "x", "confidence": 0.99})
    assert rc.classify("always do x")["category"] == "none"


def test_classify_unknown_category_is_none(rc):
    _mock_llm(rc, {"category": "banana", "scope": "global",
                   "canonical": "x", "confidence": 0.99})
    assert rc.classify("x")["category"] == "none"


# ─────────────────────────── store routing ──────────────────────────

def test_store_rule_global_md(rc):
    c = {"category": "rule", "scope": "global",
         "canonical": "always use yarn", "confidence": 0.9}
    out = rc.store(c)
    assert out["location"] == "md:rules:global"
    from aiforge_core.memory import md_store
    p = md_store._find_by_source("rules:global")
    assert p is not None and "always use yarn" in p.read_text()


def test_store_rule_project_writes_repo_rules_and_md(rc, tmp_path):
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    c = {"category": "rule", "scope": "project",
         "canonical": "use tabs here", "confidence": 0.9}
    out = rc.store(c, repo="myrepo", repo_root=str(repo_root))
    # md_store
    from aiforge_core.memory import md_store
    p = md_store._find_by_source("rules:myrepo")
    assert p is not None and "use tabs here" in p.read_text()
    # .aiforge/rules file
    rules_dir = repo_root / ".aiforge" / "rules"
    files = list(rules_dir.glob("*.md"))
    assert files and "use tabs here" in files[0].read_text()
    assert out["location"] == "md:rules:myrepo"


def test_store_session_is_isolated_and_not_persisted(rc):
    c = {"category": "rule", "scope": "session",
         "canonical": "for this chat only", "confidence": 0.9}
    out = rc.store(c, session_id=1)
    assert out["location"] == "session"
    # visible in its own session
    items1 = rc.list_captured(session_id=1)
    assert any(i["canonical"] == "for this chat only" for i in items1)
    # NOT visible to another session
    items2 = rc.list_captured(session_id=2)
    assert not any(i["canonical"] == "for this chat only" for i in items2)
    # NOT in the persistent index
    idx = json.loads((rc._index_path()).read_text()) if rc._index_path().is_file() else {"items": {}}
    assert all(it.get("scope") != "session" for it in idx["items"].values())


def test_store_memory_routes_to_memory(rc):
    c = {"category": "memory", "scope": "global",
         "canonical": "my name is sam", "confidence": 0.9}
    out = rc.store(c, repo="notes")
    assert out["location"].startswith("memory:")


# ─────────────────────────── apply_behavioral ───────────────────────

def test_apply_behavioral_commit_rule_sets_flag(rc):
    c = {"category": "rule", "scope": "global",
         "canonical": "for git commit, commit directly because the machine has access",
         "confidence": 0.9}
    flags = rc.apply_behavioral(c)
    assert "commit_auto_approve" in flags
    assert rc.flag_active("commit_auto_approve") is True


def test_apply_behavioral_delete_rule_sets_flag(rc):
    c = {"category": "rule", "scope": "global",
         "canonical": "you can delete without asking", "confidence": 0.9}
    flags = rc.apply_behavioral(c)
    assert "allow_delete" in flags
    assert rc.flag_active("allow_delete") is True


def test_apply_behavioral_arbitrary_rule_sets_no_flag(rc):
    c = {"category": "rule", "scope": "global",
         "canonical": "always write docstrings", "confidence": 0.9}
    assert rc.apply_behavioral(c) == []


def test_flag_active_precedence_session_over_repo_over_global(rc):
    # global on
    rc.apply_behavioral({"category": "rule", "scope": "global",
                         "canonical": "commit directly", "confidence": 0.9})
    assert rc.flag_active("commit_auto_approve", repo="r1", session_id=5) is True
    # repo + session also resolve (any level True → True; precedence picks first
    # defined level)
    rc.apply_behavioral({"category": "rule", "scope": "session",
                         "canonical": "commit directly", "confidence": 0.9},
                        session_id=9)
    assert rc.flag_active("commit_auto_approve", session_id=9) is True
    # a session with nothing set falls through to global
    assert rc.flag_active("commit_auto_approve", session_id=123) is True
    # unknown flag → False
    assert rc.flag_active("nope") is False


# ─────────────────────────── transparency ───────────────────────────

def test_rescope_and_undo(rc):
    c = {"category": "rule", "scope": "global",
         "canonical": "always use yarn", "confidence": 0.9}
    out = rc.store(c)
    rid = out["id"]
    # rescope global → project
    r = rc.rescope(rid, "project")
    assert r["scope"] == "project"
    items = rc.list_captured()
    assert any(i["id"] == rid and i["scope"] == "project" for i in items)
    # undo removes it
    assert rc.undo(rid) is True
    assert not any(i["id"] == rid for i in rc.list_captured())


# ─────────────────────────── gate honoring (end-to-end) ─────────────

def test_commit_auto_approve_skips_inline_gate(rc, monkeypatch, tmp_path):
    """With a global 'commit directly' rule active, a git commit that policy
    would gate (run_command=ask) runs WITHOUT an approval event."""
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_POLICY", "run_command=ask")
    # activate the flag
    rc.apply_behavioral({"category": "rule", "scope": "global",
                         "canonical": "commit directly, the machine has access",
                         "confidence": 0.9})
    from aiforge_core.runtime.chat_agent import run_chat_agent

    calls = {"n": 0}

    def fake_complete(role, convo, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return ('THOUGHT: commit\nACTION: run_command\n'
                    'ARGS_JSON: {"cmd": "git commit -m x"}')
        return "FINAL: done"

    events = list(run_chat_agent(
        [{"role": "user", "content": "go"}], cwd=str(tmp_path),
        role="chat", complete_fn=fake_complete, session_id=None))
    assert not any(e.get("type") == "approval" for e in events)
    # the tool actually executed (a tool event is present)
    assert any(e.get("type") == "tool" for e in events)


def test_without_flag_inline_gate_fires(rc, monkeypatch, tmp_path):
    """Control: same git commit with NO flag → an approval event IS emitted."""
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_POLICY", "run_command=ask")
    from aiforge_core.runtime.chat_agent import run_chat_agent

    def fake_complete(role, convo, **kw):
        return ('THOUGHT: commit\nACTION: run_command\n'
                'ARGS_JSON: {"cmd": "git commit -m x"}')

    # session_id=None → the gate auto-rejects instead of blocking, so the run
    # terminates without hanging while still proving the gate fired.
    events = list(run_chat_agent(
        [{"role": "user", "content": "go"}], cwd=str(tmp_path),
        role="chat", complete_fn=fake_complete, session_id=None))
    assert any(e.get("type") == "approval" for e in events)
