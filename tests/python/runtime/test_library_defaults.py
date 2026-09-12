"""Shipped defaults are the company's: this box may disable one, never edit or
delete the file that ships inside the package."""
from __future__ import annotations

import pytest


@pytest.fixture
def box(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _a_builtin(load_fn):
    from aiforge_core.runtime import library_defaults
    for item in load_fn():
        if library_defaults.is_builtin(getattr(item, "source", "")):
            return item
    return None


def test_a_builtin_source_is_recognised(box):
    from pathlib import Path

    from aiforge_core.runtime import library_defaults, skills
    assert library_defaults.is_builtin("builtin") is True
    assert library_defaults.is_builtin(
        str(Path(skills.__file__).parent / "builtin_playbooks" / "skills"
            / "x" / "SKILL.md")) is True
    assert library_defaults.is_builtin(str(box / "skills" / "mine" / "SKILL.md")) \
        is False
    assert library_defaults.is_builtin("") is False


def test_deleting_a_default_skill_disables_it_and_keeps_the_file(box):
    from aiforge_core.runtime import library_defaults, skills
    sk = _a_builtin(skills.load)
    if sk is None:
        pytest.skip("no builtin skills shipped")
    # A shipped skill's ``source`` is the sentinel "builtin", not a path (see
    # skills._load), so what has to survive is the package's own directory —
    # check the FILES, not sk.source.
    shipped = sorted(p for p in skills._builtin_dir().rglob("*.md"))
    res = skills.delete_skill(sk.name)
    assert res["ok"] is True
    assert res["disabled"] is True
    assert res["removed"] == []
    assert sorted(p for p in skills._builtin_dir().rglob("*.md")) == shipped, \
        "the shipped files must never be unlinked"
    assert sk.name in library_defaults.disabled("skill")
    assert sk.name not in {s.name for s in skills.load()}
    # …and it comes back
    assert library_defaults.enable("skill", sk.name) is True
    assert sk.name in {s.name for s in skills.load()}


def test_clearing_skills_leaves_the_defaults_alone(box):
    from aiforge_core.runtime import library_defaults, skills
    before = {s.name for s in skills.load()
              if library_defaults.is_builtin(getattr(s, "source", ""))}
    if not before:
        pytest.skip("no builtin skills shipped")
    skills.write_skill("my own skill", "mine", "do the thing")
    skills.clear_skills()
    after = {s.name for s in skills.load()}
    assert before <= after, "clear removed a shipped default"
    assert "my own skill" not in after


def test_a_custom_skill_of_the_same_name_still_overrides(box):
    from aiforge_core.runtime import skills
    sk = _a_builtin(skills.load)
    if sk is None:
        pytest.skip("no builtin skills shipped")
    skills.write_skill(sk.name, "mine", "my body wins")
    got = next(s for s in skills.load() if s.name == sk.name)
    assert "my body wins" in got.body
    assert got.source != "builtin"


def test_deleting_a_default_rule_disables_it(box):
    from aiforge_core.runtime import library_defaults, repo_rules
    r = _a_builtin(repo_rules.load_global_and_builtin)
    if r is None:
        pytest.skip("no builtin rules shipped")
    res = repo_rules.delete_rule(r.name)
    assert res["ok"] is True
    assert res["disabled"] is True
    assert r.name not in {x.name for x in repo_rules.load_global_and_builtin()}
    assert r.name in library_defaults.disabled("rule")


def test_deleting_a_default_workflow_disables_it(box):
    from aiforge_core.runtime import library_defaults, workflows
    wf = _a_builtin(workflows.load)
    if wf is None:
        pytest.skip("no builtin workflows shipped")
    res = workflows.delete_workflow(wf.name)
    assert res["ok"] is True
    assert res["disabled"] is True
    assert wf.name in library_defaults.disabled("workflow")
    assert wf.name not in {w.name for w in workflows.load()}
