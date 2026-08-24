"""Library delete/clear: a user can remove individual skills/workflows/rules
and clear a whole kind. Backs the /api/library DELETE endpoints + UI buttons.
"""
from __future__ import annotations

import tempfile

import pytest


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", tempfile.mkdtemp())
    return None


def test_delete_skill_removes_file(cfg):
    from aiforge_core.runtime import skills
    skills.write_skill("temp", "d", "body", ["t"])
    assert any(s.name == "temp" for s in skills.load())
    res = skills.delete_skill("temp")
    assert res["ok"]
    assert res["removed"]
    assert not any(s.name == "temp" for s in skills.load())
    # deleting a missing one is a clean error, not a crash
    assert not skills.delete_skill("nope")["ok"]


def test_clear_workflows(cfg):
    from aiforge_core.runtime import workflows
    workflows.write_workflow("a", "d", "steps", ["x"])
    workflows.write_workflow("b", "d", "steps", ["y"])
    # load() also surfaces SHIPPED builtin playbooks — count only user-authored.
    customs = [w for w in workflows.load() if w.source != "builtin"]
    assert len(customs) == 2
    res = workflows.clear_workflows()
    assert res["ok"]
    assert res["removed"] == 2
    # clear removes the user's workflows; undeletable builtins remain.
    assert [w for w in workflows.load() if w.source != "builtin"] == []


def test_delete_and_clear_rules(cfg):
    from aiforge_core.runtime import repo_rules
    repo_rules.write_rule("r1", "- do x")
    repo_rules.write_rule("r2", "- do y")
    assert len(repo_rules.load_global_rules()) == 2
    assert repo_rules.delete_rule("r1")["ok"]
    assert {r.name for r in repo_rules.load_global_rules()} == {"r2"}
    assert repo_rules.clear_rules()["removed"] == 1
    assert repo_rules.load_global_rules() == []


def test_delete_bounded_to_playbook_dirs(cfg, tmp_path):
    # A skill whose source is OUTSIDE the playbook dirs is never unlinked.
    from aiforge_core.runtime import skills
    outside = tmp_path / "secret.md"
    outside.write_text("---\nname: x\n---\nbody\n")
    # No such skill is loaded (outside any root), so delete finds nothing.
    assert not skills.delete_skill("x")["ok"]
    assert outside.exists()
