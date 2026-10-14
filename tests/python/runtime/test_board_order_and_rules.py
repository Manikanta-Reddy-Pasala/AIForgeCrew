"""The task board keeps the order the agent means (insert, move, remove), and
a project's rules are kept and checked."""
from __future__ import annotations

import pytest

from aiforge_core.runtime.chat_agent._turn import _plan_llm
from aiforge_core.runtime.chat_agent._turn._tasks import apply_progress, seed_board


def _board():
    return seed_board(["read the spec", "write the code", "run the tests"])


# ── board ────────────────────────────────────────────────────────────────────

def test_new_item_goes_where_it_is_placed():
    b = _board()
    res, ev = apply_progress(b, {"slug": "lint", "title": "lint the code", "after": "part-2"})
    assert list(b) == ["part-1", "part-2", "lint", "part-3"]
    assert res["position"] == 3 and ev[0]["type"] == "subtasks"
    apply_progress(b, {"slug": "setup", "title": "set up the venv", "position": 1})
    assert list(b)[0] == "setup"
    apply_progress(b, {"slug": "notes", "title": "write notes", "before": "part-1"})
    assert list(b).index("notes") == list(b).index("part-1") - 1


def test_known_item_can_be_moved_without_changing_status():
    b = _board()
    apply_progress(b, {"slug": "part-2", "status": "running"})
    apply_progress(b, {"slug": "part-3", "before": "part-2"})
    assert list(b) == ["part-1", "part-3", "part-2"]
    assert b["part-3"]["status"] == "pending"          # moving is not "done"


def test_order_resorts_the_board():
    b = _board()
    res, ev = apply_progress(b, {"order": ["part-3", "part-1"]})
    assert list(b) == ["part-3", "part-1", "part-2"] and ev


def test_remove_takes_an_item_off():
    b = _board()
    apply_progress(b, {"slug": "extra", "title": "an extra step"})
    res, ev = apply_progress(b, {"slug": "extra", "remove": True})
    assert res["removed"] and "extra" not in b and ev[0]["type"] == "subtasks"
    res, _ = apply_progress(b, {"slug": "nope", "status": "removed"})
    assert not res["ok"]


def test_a_part_of_the_request_is_removed_only_when_the_user_dropped_it():
    b = _board()
    res, _ = apply_progress(b, {"slug": "part-2", "remove": True})
    assert not res["ok"] and "part-2" in b
    res, _ = apply_progress(b, {"slug": "part-2", "remove": True}, allow_request_removal=True)
    assert res["ok"] and "part-2" not in b


def test_revise_removes_dropped_work_and_keeps_order():
    b = _board()
    new, changes = _plan_llm._merged(b, [
        {"id": "part-1", "status": "pending", "title": "read the spec"},
        {"id": "new", "title": "set up docker"},
        {"id": "part-2", "status": "skipped", "title": "write the code"},
        {"id": "part-3", "status": "pending", "title": "run the tests"},
    ])
    assert "part-2" not in new
    assert list(new)[0] == "part-1" and list(new)[-1] == "part-3"
    assert any(c.startswith("removed part-2") for c in changes)


# ── rules ────────────────────────────────────────────────────────────────────

@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    from aiforge_core.runtime.chat_agent._tools import _memory
    _memory._source_path_cache.clear()
    return tmp_path


def test_project_rule_with_triggers_is_always_on(cfg, monkeypatch):
    from aiforge_core.memory import md_store
    from aiforge_core.runtime.chat_agent._tools import _memory
    monkeypatch.setattr(_memory, "_chat_repo_key", lambda cwd: "shop")
    md_store.append_bullet(source="rules:shop", title="shop — rules",
                           bullet="[triggers: vm, ssh, docker] Run tests only on the VM.",
                           kind="rule", tags=["rule", "project"])
    always, tagged = _memory._md_rule_bullets("/x/shop")
    assert any("Run tests only on the VM" in a for a in always) and not tagged
    assert "Run tests only on the VM" in _memory._rules_context("/x/shop", "run the tests")


def test_captured_project_rule_has_no_topic_gate(cfg, tmp_path):
    from aiforge_core.runtime.rule_capture import _store
    item = _store._new_item("r1", {"category": "rule", "scope": "project",
                                   "canonical": "Run tests only on the VM"}, "shop", 1)
    repo = tmp_path / "shop"
    repo.mkdir()
    _store._store_rule(item, {"triggers": ["vm", "ssh"]}, "shop", str(repo))
    assert item["triggers"] == []
    body = (repo / ".aiforge" / "rules").glob("*.md").__next__().read_text()
    assert "alwaysApply: true" in body and "triggers" not in body


def test_project_words_keep_a_preference_in_the_project():
    from aiforge_core.runtime import preference_capture as pc
    assert pc._project_scoped("for this project always use the VM", "shop")
    assert pc._project_scoped("in shop, databases run in docker", "shop")
    assert not pc._project_scoped("always answer briefly", "shop")
    assert not pc._project_scoped("for this project use the VM", None)


def test_rules_header_forbids_the_fallback(cfg, monkeypatch):
    from aiforge_core.runtime.chat_agent._tools import _memory
    monkeypatch.setattr(_memory, "_md_rule_bullets", lambda cwd: (["- run on the VM"], []))
    text = _memory._rules_context("/x", "")
    assert "never do what the rule forbids" in text


# ── the rule-break check ─────────────────────────────────────────────────────

def test_breaks_parses_the_check(monkeypatch):
    from aiforge_core.runtime.chat_agent._guards import rules as rg
    monkeypatch.setattr(_plan_llm, "_ask",
                        lambda *a, **k: 'x [{"action": 2, "rule": "tests only on the VM"}]')
    got = rg.breaks("chat", ["- run tests only on the VM"],
                    ["ssh testvm 'pytest'", "python -m pytest"], 1)
    assert got == [("python -m pytest", "tests only on the VM")]
    monkeypatch.setattr(_plan_llm, "_ask", lambda *a, **k: None)
    assert rg.breaks("chat", ["- r"], ["c"], 1) == []
    assert rg.breaks("chat", [], ["c"], 1) == []


def test_guard_sends_back_then_labels(monkeypatch):
    from aiforge_core.runtime.chat_agent._guards import base
    from aiforge_core.runtime.chat_agent._guards import rules as rg
    from aiforge_core.runtime.chat_agent._tools import _memory
    monkeypatch.setattr(_memory, "user_rule_lines", lambda cwd: ["- tests only on the VM"])
    monkeypatch.setattr(rg, "_commands", lambda sid, since=0: ["python -m pytest"][since:])
    monkeypatch.setattr(rg, "breaks", lambda role, rules, cmds, sid:
                        [(c, "tests only on the VM") for c in cmds])

    class St:
        session_id = 5
        role = "chat"
        convo: list = []

    st = St()
    st.convo = []
    g = rg.RuleBreakGuard("/x", False, "", False)
    step = {"text": "All tests pass."}
    events = list(base._check(g, st, step))
    assert st.convo and "rule check" in st.convo[-1]["content"]
    assert st.rule_check_mark == 1
    # after the nudge only new commands count: none, so no label
    step2 = {"text": "The VM is unreachable; I could not run the tests."}
    list(base._check(g, st, step2))
    assert not step2["text"].startswith("⚠")
    assert events


# ── review fixes ─────────────────────────────────────────────────────────────

def test_finished_items_stay_as_the_record():
    b = _board()
    apply_progress(b, {"slug": "extra", "title": "an extra step", "status": "done"})
    res, _ = apply_progress(b, {"slug": "extra", "remove": True})
    assert not res["ok"] and "extra" in b


def test_reviser_listing_a_dropped_id_twice_does_not_crash():
    b = _board()
    new, changes = _plan_llm._merged(b, [
        {"id": "part-1", "status": "skipped", "title": "read the spec"},
        {"id": "part-1", "title": "read it again"}])
    assert "part-1" not in new


def test_a_removed_slug_is_not_handed_out_again():
    b = {"part-1": {"title": "a", "status": "pending"},
         "msg-2": {"title": "b", "status": "pending", "by_harness": True}}
    new, _ = _plan_llm._merged(b, [
        {"id": "part-1", "status": "pending", "title": "a"},
        {"id": "msg-2", "status": "skipped", "title": "b"},
        {"id": None, "title": "c new work"}])
    assert "msg-2" not in new and any(v["title"] == "c new work" for v in new.values())


def test_guard_keeps_the_break_when_the_model_reruns_nothing(monkeypatch):
    from aiforge_core.runtime.chat_agent._guards import base
    from aiforge_core.runtime.chat_agent._guards import rules as rg
    from aiforge_core.runtime.chat_agent._tools import _memory
    monkeypatch.setattr(_memory, "user_rule_lines", lambda cwd: ["- tests only on the VM"])
    monkeypatch.setattr(rg, "_commands", lambda sid, since=0: ["python -m pytest"][since:])
    monkeypatch.setattr(rg, "breaks", lambda role, rules, cmds, sid:
                        [(c, "tests only on the VM") for c in cmds])

    class St:
        session_id = 5
        role = "chat"
    st = St()
    st.convo = []
    g = rg.RuleBreakGuard("/x", False, "", False)
    list(base._check(g, st, {"text": "All tests pass."}))
    step = {"text": "All tests pass."}                   # same claim, nothing new run
    list(base._check(g, st, step))
    assert step["text"].startswith("⚠ **Harness rule check:**")
    assert "python -m pytest" in step["text"]


def test_project_preference_stays_in_its_project(cfg, monkeypatch):
    from aiforge_core.memory import sqlite_memory as m
    from aiforge_core.runtime.chat_agent._tools import _memory
    m.upsert_by_tag(text="use the VM", tag="pref:vm", kind="preference",
                    source="preference", tags=["preference"], metadata={}, repo="shop")
    m.upsert_by_tag(text="answer briefly", tag="pref:brief", kind="preference",
                    source="preference", tags=["preference"], metadata={}, repo=None)
    shop = " ".join(_memory._preference_lines("shop"))
    other = " ".join(_memory._preference_lines("other"))
    assert "use the VM" in shop and "answer briefly" in shop
    assert "use the VM" not in other and "answer briefly" in other


def test_remember_rule_project_scope_writes_into_the_project(cfg, tmp_path, monkeypatch):
    import subprocess
    from aiforge_core.runtime.chat_agent._tools import _memory
    repo = tmp_path / "shop"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.setattr(_memory, "_elaborate_body", lambda kind, text, **k: text)
    monkeypatch.setattr(_memory, "_record_rule_memory", lambda *a, **k: None)
    res = _memory._t_remember_rule({"text": "run tests only on the VM",
                                    "scope": "project"}, str(repo))
    assert res["ok"] and str(repo) in res["path"] and ".aiforge/rules" in res["path"]
    from aiforge_core.runtime import repo_rules
    assert not list((repo_rules._global_rules_dir()).glob("*.md")) \
        if repo_rules._global_rules_dir().exists() else True
