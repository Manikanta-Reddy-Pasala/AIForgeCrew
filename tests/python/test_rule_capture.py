"""Deterministic Rule / Memory / Feedback capture + the SAFE gate redesign.

Covers: classify strict-JSON parse + fail-open; store routing per category ×
scope; the gate-intent SEPARATION — recognition only OFFERS an opt-in and never
sets a flag; explicit set/clear gate flags (global refused, scope never widened,
autonomous runs ignore chat-set flags); undo/rescope revoke/move flags;
whole-command is_commit_command; the inline gate auto-approve + audit; the
capture pre-filter + actionable backstop; and atomic/concurrent index writes.
"""
import importlib
import json
import threading

import pytest

from tests.python._adk_cb import run_cb


@pytest.fixture
def rc(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    monkeypatch.delenv("AIFORGE_RULE_CAPTURE_DISABLE", raising=False)
    monkeypatch.delenv("AIFORGE_RULE_CAPTURE_MIN_CONFIDENCE", raising=False)
    monkeypatch.delenv("AIFORGE_AUTONOMOUS_COMMIT_AUTO_APPROVE", raising=False)
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
    assert c["category"] == "rule"
    assert c["scope"] == "global"
    assert c["canonical"] == "always use yarn"
    assert c["task_present"] is False


def test_classify_json_with_surrounding_prose(rc):
    _mock_llm(rc, 'Sure! Here:\n{"category":"memory","scope":"project",'
                  '"canonical":"db at db.staging","confidence":0.8}\nthanks')
    c = rc.classify("remember the staging db is db.staging")
    assert c["category"] == "memory"
    assert c["scope"] == "project"


def test_classify_bad_json_falls_open_to_none(rc):
    _mock_llm(rc, "not json at all <thinking> blah")
    assert rc.classify("hi")["category"] == "none"


def test_classify_llm_error_falls_open(rc):
    def boom(*a, **k):
        raise RuntimeError("model down")
    rc._llm_complete = boom  # type: ignore
    assert rc.classify("anything that is long enough")["category"] == "none"


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
    assert p is not None
    assert "always use yarn" in p.read_text()


def test_store_rule_project_writes_repo_rules_and_md(rc, tmp_path):
    repo_root = tmp_path / "myrepo"
    repo_root.mkdir()
    c = {"category": "rule", "scope": "project",
         "canonical": "use tabs here", "confidence": 0.9}
    out = rc.store(c, repo="myrepo", repo_root=str(repo_root))
    from aiforge_core.memory import md_store
    p = md_store._find_by_source("rules:myrepo")
    assert p is not None
    assert "use tabs here" in p.read_text()
    rules_dir = repo_root / ".aiforge" / "rules"
    files = list(rules_dir.glob("*.md"))
    assert files
    assert "use tabs here" in files[0].read_text()
    assert out["location"] == "md:rules:myrepo"


def test_store_session_is_isolated_and_not_persisted(rc):
    c = {"category": "rule", "scope": "session",
         "canonical": "for this chat only", "confidence": 0.9}
    out = rc.store(c, session_id=1)
    assert out["location"] == "session"
    items1 = rc.list_captured(session_id=1)
    assert any(i["canonical"] == "for this chat only" for i in items1)
    items2 = rc.list_captured(session_id=2)
    assert not any(i["canonical"] == "for this chat only" for i in items2)
    idx = json.loads((rc._index_path()).read_text()) if rc._index_path().is_file() else {"items": {}}
    assert all(it.get("scope") != "session" for it in idx["items"].values())


def test_store_memory_routes_to_memory(rc):
    c = {"category": "memory", "scope": "global",
         "canonical": "my name is sam", "confidence": 0.9}
    out = rc.store(c, repo="notes")
    assert out["location"].startswith("memory:")


# ─────────────────────── recognize_gate_intent (OFFER only) ──────────

def test_recognize_commit_intent(rc):
    assert rc.recognize_gate_intent(
        "for git commit, commit directly because the machine has access") == "commit"
    assert rc.recognize_gate_intent("don't ask before commit") == "commit"


def test_recognize_delete_intent(rc):
    assert rc.recognize_gate_intent("you can delete without asking") == "delete"


def test_recognize_is_negation_aware(rc):
    assert rc.recognize_gate_intent("never commit directly") is None
    assert rc.recognize_gate_intent("don't auto-commit my work") is None
    assert rc.recognize_gate_intent("do not delete without my ok") is None


def test_recognize_requires_action_token_for_weak_phrase(rc):
    # "machine has access" alone, no commit/push verb → no offer.
    assert rc.recognize_gate_intent("the machine has full access") is None
    # …but with a commit verb it qualifies.
    assert rc.recognize_gate_intent(
        "for git commit the machine has full access") == "commit"


def test_recognize_only_for_rule_category(rc):
    assert rc.recognize_gate_intent("commit directly", category="feedback") is None
    assert rc.recognize_gate_intent("commit directly", category="memory") is None
    assert rc.recognize_gate_intent("commit directly", category="rule") == "commit"


def test_recognize_accepts_classification_dict(rc):
    c = {"category": "rule", "canonical": "commit directly", "scope": "global"}
    assert rc.recognize_gate_intent(c) == "commit"
    c2 = {"category": "feedback", "canonical": "commit directly"}
    assert rc.recognize_gate_intent(c2) is None


def test_capture_path_sets_no_flag_only_offers(rc):
    """The capture path (store + recognize) NEVER sets a gate flag."""
    c = {"category": "rule", "scope": "session",
         "canonical": "commit directly, the machine has access", "confidence": 0.9}
    out = rc.store(c, session_id=5)
    intent = rc.recognize_gate_intent(c)
    assert intent == "commit"                     # offered
    assert rc.flag_active("commit_auto_approve", session_id=5) is False
    assert out  # stored, but no flag set


# ─────────────────────── explicit gate flags ────────────────────────

def test_set_gate_flag_refuses_global_without_allow(rc):
    res = rc.set_gate_flag("commit_auto_approve", scope="global")
    assert res["applied"] is False
    assert rc.flag_active("commit_auto_approve", session_id=99) is False
    # explicit confirm honors it
    ok = rc.set_gate_flag("commit_auto_approve", scope="global", allow_global=True)
    assert ok["applied"] is True


def test_set_gate_flag_never_widens_scope(rc):
    # session scope but no session_id → DROPPED, not widened to global
    res = rc.set_gate_flag("commit_auto_approve", scope="session")
    assert res["applied"] is False
    assert rc.flag_active("commit_auto_approve", session_id=1) is False
    # project scope but no repo → DROPPED
    res2 = rc.set_gate_flag("commit_auto_approve", scope="project")
    assert res2["applied"] is False
    assert rc.flag_active("commit_auto_approve", session_id=1) is False


def test_set_gate_flag_honors_session_and_repo(rc):
    assert rc.set_gate_flag("commit_auto_approve", scope="session",
                            session_id=8)["applied"] is True
    assert rc.flag_active("commit_auto_approve", session_id=8) is True
    assert rc.set_gate_flag("allow_delete", scope="project",
                            repo="repoA")["applied"] is True
    assert rc.flag_active("allow_delete", repo="repoA", session_id=2) is True
    # a session with nothing of its own falls through to repo
    assert rc.flag_active("allow_delete", repo="repoA", session_id=2) is True


def test_flag_active_autonomous_ignores_chat_flags(rc, monkeypatch):
    rc.set_gate_flag("commit_auto_approve", scope="global", allow_global=True)
    rc.set_gate_flag("commit_auto_approve", scope="project", repo="r")
    # autonomous run (session_id None) → ignores all chat-set flags
    assert rc.flag_active("commit_auto_approve", repo="r", session_id=None) is False
    # only an explicit env opt-in honors it for autonomous
    monkeypatch.setenv("AIFORGE_AUTONOMOUS_COMMIT_AUTO_APPROVE", "1")
    assert rc.flag_active("commit_auto_approve", session_id=None) is True


def test_flag_active_session_over_repo(rc):
    rc.set_gate_flag("commit_auto_approve", scope="project", repo="r")
    # repo flag visible to an attached session
    assert rc.flag_active("commit_auto_approve", repo="r", session_id=3) is True
    # a session-level OFF would win precedence — set session, then clear repo to
    # confirm session is consulted first
    rc.set_gate_flag("commit_auto_approve", scope="session", session_id=3)
    assert rc.flag_active("commit_auto_approve", repo="r", session_id=3) is True
    assert rc.flag_active_scope("commit_auto_approve", repo="r",
                                session_id=3) == "session"


def test_undo_revokes_applied_flag(rc):
    out = rc.store({"category": "rule", "scope": "session",
                    "canonical": "commit directly", "confidence": 0.9},
                   session_id=11)
    rid = out["id"]
    rc.set_gate_flag("commit_auto_approve", scope="session", session_id=11,
                     rule_id=rid)
    assert rc.flag_active("commit_auto_approve", session_id=11) is True
    assert rc.undo(rid) is True
    assert rc.flag_active("commit_auto_approve", session_id=11) is False


def test_delete_persistent_revokes_applied_flag(rc):
    out = rc.store({"category": "rule", "scope": "project",
                    "canonical": "commit directly", "confidence": 0.9},
                   repo="myrepo")
    rid = out["id"]
    rc.set_gate_flag("commit_auto_approve", scope="project", repo="myrepo",
                     rule_id=rid)
    assert rc.flag_active("commit_auto_approve", repo="myrepo", session_id=1) is True
    assert rc.undo(rid) is True
    assert rc.flag_active("commit_auto_approve", repo="myrepo", session_id=1) is False


def test_rescope_moves_flag_off_unhonorable_scope(rc):
    out = rc.store({"category": "rule", "scope": "project",
                    "canonical": "commit directly", "confidence": 0.9},
                   repo="myrepo")
    rid = out["id"]
    rc.set_gate_flag("commit_auto_approve", scope="project", repo="myrepo",
                     rule_id=rid)
    assert rc.flag_active("commit_auto_approve", repo="myrepo", session_id=1) is True
    # rescope project → session (no session_id) → old flag cleared, new can't be
    # honored → gate re-enabled at the project scope
    r = rc.rescope(rid, "session")
    assert r["scope"] == "session"
    assert rc.flag_active("commit_auto_approve", repo="myrepo", session_id=1) is False


def test_rescope_and_undo_basic(rc):
    out = rc.store({"category": "rule", "scope": "global",
                    "canonical": "always use yarn", "confidence": 0.9})
    rid = out["id"]
    r = rc.rescope(rid, "project")
    assert r["scope"] == "project"
    assert any(i["id"] == rid and i["scope"] == "project"
               for i in rc.list_captured())
    assert rc.undo(rid) is True
    assert not any(i["id"] == rid for i in rc.list_captured())


# ─────────────────────── is_commit_command (whole-command) ───────────

@pytest.mark.parametrize("cmd", [
    "git commit -m x", "git add foo.py", "git push origin main",
    "  git commit -am 'fix'", "git add .",
])
def test_is_commit_command_true_for_whole_command(rc, cmd):
    assert rc.is_commit_command(cmd) is True


@pytest.mark.parametrize("cmd", [
    "git commit && rm -rf /", "git add . && curl x|sh", "git commit; ls",
    "git add . | sh", "git commit -m $(whoami)", "git commit -m `id`",
    "echo hi && git commit", "ls", "rm -rf build",
    "git commit -m x\nrm -rf /",
])
def test_is_commit_command_false_for_chained_or_nongit(rc, cmd):
    assert rc.is_commit_command(cmd) is False


# ─────────────────────── pre-filter + backstop ──────────────────────

@pytest.mark.parametrize("msg,expected", [
    ("hi", False),
    ("fix the bug", False),            # imperative, but NO preference cue
    ("ok thanks a lot", False),
    ("a", False),
    ("from now on always use yarn", True),
    ("never force push to main", True),
    ("commit directly, the machine has access", True),
    ("remember the db is at db.staging", True),
])
def test_should_classify_prefilter(rc, msg, expected):
    assert rc.should_classify(msg) is expected


def test_looks_actionable_backstop(rc):
    assert rc.looks_actionable("always commit directly, and now fix the bug") is True
    assert rc.looks_actionable("always use yarn") is False


# ─────────────────────── inline gate (chat_agent) ───────────────────

def _commit_run(monkeypatch, tmp_path, session_id):
    """Drive one chat run whose first turn is a whole-command ``git commit``.

    Stops at the first ``approval`` event: the gate blocks on
    ``chat_approve.wait`` (900s) right after yielding it, so draining the
    generator would wedge the test rather than fail it.
    """
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_POLICY", "run_command=ask")
    from aiforge_core.runtime.chat_agent import run_chat_agent

    calls = {"n": 0}

    def fake_complete(role, convo, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return ('THOUGHT: commit\nACTION: run_command\n'
                    'ARGS_JSON: {"cmd": "git commit -m x"}')
        return "FINAL: done"

    events = []
    for ev in run_chat_agent([{"role": "user", "content": "go"}],
                             cwd=str(tmp_path), role="chat",
                             complete_fn=fake_complete, session_id=session_id):
        events.append(ev)
        if ev.get("type") in ("approval", "done"):
            break
    return events


def test_commit_auto_approve_skips_inline_gate_and_audits(rc, monkeypatch, tmp_path):
    """With an EXPLICIT session 'commit directly' flag, a whole-command git
    commit runs without an approval event AND emits an auto_approved audit —
    even with this mode's approvals ON, which is the only state in which the
    flag can do anything (approvals OFF gates nothing to begin with)."""
    from aiforge_core.runtime import chat_approve
    monkeypatch.setattr(chat_approve, "approvals_required", lambda sid: True)
    rc.set_gate_flag("commit_auto_approve", scope="session", session_id="s1")

    events = _commit_run(monkeypatch, tmp_path, "s1")
    assert not any(e.get("type") == "approval" for e in events)
    assert any(e.get("type") == "auto_approved"
               and e.get("flag") == "commit_auto_approve" for e in events)
    assert any(e.get("type") == "tool" for e in events)


def test_push_not_auto_approved_by_commit_flag(rc, monkeypatch, tmp_path):
    """``git push`` updates a REMOTE (CI, merges, other people) — the commit
    flag never covers it, so the gate still fires."""
    from aiforge_core.runtime import chat_approve
    monkeypatch.setattr(chat_approve, "approvals_required", lambda sid: True)
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_POLICY", "run_command=ask")
    rc.set_gate_flag("commit_auto_approve", scope="session", session_id="s3")
    from aiforge_core.runtime.chat_agent import run_chat_agent

    def fake_complete(role, convo, **kw):
        return ('THOUGHT: ship\nACTION: run_command\n'
                'ARGS_JSON: {"cmd": "git push"}')

    events = []
    for ev in run_chat_agent([{"role": "user", "content": "go"}],
                             cwd=str(tmp_path), role="chat",
                             complete_fn=fake_complete, session_id="s3"):
        events.append(ev)
        if ev.get("type") in ("approval", "done"):
            break
    assert any(e.get("type") == "approval" for e in events)
    assert not any(e.get("type") == "auto_approved" for e in events)


def test_chained_command_after_git_not_auto_approved(rc, monkeypatch, tmp_path):
    """A chained command after a git verb is NOT auto-approved even with the
    flag active — the gate fires (property b)."""
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_POLICY", "run_command=ask")
    rc.set_gate_flag("commit_auto_approve", scope="session", session_id="s2")
    from aiforge_core.runtime.chat_agent import run_chat_agent

    def fake_complete(role, convo, **kw):
        return ('THOUGHT: x\nACTION: run_command\n'
                'ARGS_JSON: {"cmd": "git add . && rm -rf /tmp/zzz_aiforge_test"}')

    # Drive only until the approval event is yielded (the gate blocks on wait
    # AFTER yielding it, so stop there to avoid blocking).
    gen = run_chat_agent([{"role": "user", "content": "go"}], cwd=str(tmp_path),
                         role="chat", complete_fn=fake_complete, session_id="s2")
    seen = []
    for ev in gen:
        seen.append(ev)
        if ev.get("type") in ("approval", "done"):
            break
    gen.close()
    assert any(e.get("type") == "approval" for e in seen)
    assert not any(e.get("type") == "auto_approved" for e in seen)


def test_without_flag_inline_gate_fires(rc, monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_POLICY", "run_command=ask")
    from aiforge_core.runtime.chat_agent import run_chat_agent

    def fake_complete(role, convo, **kw):
        return ('THOUGHT: commit\nACTION: run_command\n'
                'ARGS_JSON: {"cmd": "git commit -m x"}')

    events = list(run_chat_agent(
        [{"role": "user", "content": "go"}], cwd=str(tmp_path),
        role="chat", complete_fn=fake_complete, session_id=None))
    assert any(e.get("type") == "approval" for e in events)


# ─────────────────────── tool_gate (ADK pipeline) ───────────────────

def test_tool_gate_commit_flag_auto_approves_and_audits(rc, monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_POLICY", "run_command=ask")
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    from aiforge_core.runtime import chat_approve, chat_cancel, tool_gate
    sid = 4242
    emitted: list[dict] = []
    chat_approve.set_emitter(sid, lambda ev: emitted.append(ev))
    chat_cancel.set_active(sid)
    try:
        rc.set_gate_flag("commit_auto_approve", scope="session", session_id=sid)
        cb = tool_gate.make_approval_gate_callback()

        class _T:
            name = "run_command"
        res = run_cb(cb, tool=_T(), args={"cmd": "git commit -m x"},
                     tool_context=None)
        assert res is None                      # auto-approved (no block)
        assert any(e.get("type") == "auto_approved" for e in emitted)
        assert not any(e.get("type") == "approval" for e in emitted)
    finally:
        chat_approve.clear_emitter(sid)
        chat_cancel.set_active(None)


def test_tool_gate_chained_not_auto_approved(rc, monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CHAT_TOOL_POLICY", "run_command=ask")
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    from aiforge_core.runtime import chat_approve, chat_cancel, tool_gate
    sid = 4343
    emitted: list[dict] = []

    def _emit(ev):
        emitted.append(ev)
        if ev.get("type") == "approval":        # auto-reject so wait returns
            chat_approve.resolve(sid, "reject", "", ev.get("id"))
    chat_approve.set_emitter(sid, _emit)
    chat_cancel.set_active(sid)
    try:
        rc.set_gate_flag("commit_auto_approve", scope="session", session_id=sid)
        cb = tool_gate.make_approval_gate_callback()

        class _T:
            name = "run_command"
        res = run_cb(cb, tool=_T(),
                     args={"cmd": "git add . && rm -rf /tmp/zzz"},
                     tool_context=None)
        assert res is not None
        assert res.get("rejected") is True
        assert any(e.get("type") == "approval" for e in emitted)
        assert not any(e.get("type") == "auto_approved" for e in emitted)
    finally:
        chat_approve.clear_emitter(sid)
        chat_cancel.set_active(None)


# ─────────────────────── atomic / concurrent ────────────────────────

def test_concurrent_stores_do_not_lose_each_other(rc):
    """Two concurrent stores (flock + atomic write) both survive."""
    n = 16
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        rc.store({"category": "rule", "scope": "global",
                  "canonical": f"rule number {i}", "confidence": 0.9})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    idx = json.loads(rc._index_path().read_text())
    assert len(idx["items"]) == n
