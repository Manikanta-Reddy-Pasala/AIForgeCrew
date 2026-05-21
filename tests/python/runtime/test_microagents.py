from __future__ import annotations

import pytest

from aiforge_core.runtime import microagents as ma


@pytest.fixture
def micro_dir(tmp_path, monkeypatch):
    d = tmp_path / "micros"
    d.mkdir()
    monkeypatch.setenv("AIFORGE_MICROAGENTS_DIR", str(d))
    return d


def _write(d, name, body):
    (d / name).write_text(body, encoding="utf-8")


def test_load_empty_dir_returns_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MICROAGENTS_DIR", str(tmp_path / "nope"))
    assert ma.load_microagents() == []


def test_load_parses_frontmatter(micro_dir):
    _write(micro_dir, "pytest_tips.md", (
        "---\n"
        "name: pytest-tips\n"
        "type: knowledge\n"
        "triggers: [pytest, fixture, conftest]\n"
        "priority: 5\n"
        "---\n"
        "Use conftest.py for shared fixtures.\n"
    ))
    agents = ma.load_microagents()
    assert len(agents) == 1
    a = agents[0]
    assert a.name == "pytest-tips"
    assert a.type == "knowledge"
    assert a.triggers == ("pytest", "fixture", "conftest")
    assert a.priority == 5
    assert "conftest.py" in a.body


def test_load_skips_files_without_frontmatter(micro_dir):
    _write(micro_dir, "bad.md", "no frontmatter here\n")
    assert ma.load_microagents() == []


def test_load_skips_files_with_no_triggers(micro_dir):
    _write(micro_dir, "no_trig.md",
           "---\nname: x\ntype: knowledge\ntriggers: []\n---\nbody\n")
    assert ma.load_microagents() == []


def test_match_returns_matching_agent(micro_dir):
    _write(micro_dir, "a.md",
           "---\nname: a\ntype: knowledge\ntriggers: [pytest]\npriority: 1\n"
           "---\nbody A\n")
    _write(micro_dir, "b.md",
           "---\nname: b\ntype: knowledge\ntriggers: [docker]\npriority: 1\n"
           "---\nbody B\n")
    agents = ma.load_microagents()
    hits = ma.match("running pytest in CI", agents)
    assert len(hits) == 1
    assert hits[0].name == "a"


def test_match_is_case_insensitive(micro_dir):
    _write(micro_dir, "a.md",
           "---\nname: a\ntype: knowledge\ntriggers: [Docker]\npriority: 1\n"
           "---\nbody\n")
    agents = ma.load_microagents()
    assert ma.match("DOCKER compose up", agents)
    assert ma.match("docker run", agents)


def test_match_priority_sort(micro_dir):
    _write(micro_dir, "low.md",
           "---\nname: low\ntype: knowledge\ntriggers: [pytest]\npriority: 1\n"
           "---\nlow\n")
    _write(micro_dir, "high.md",
           "---\nname: high\ntype: knowledge\ntriggers: [pytest]\npriority: 10\n"
           "---\nhigh\n")
    agents = ma.load_microagents()
    hits = ma.match("pytest", agents)
    assert [h.name for h in hits] == ["high", "low"]


def test_render_injection_delimits(micro_dir):
    _write(micro_dir, "a.md",
           "---\nname: a\ntype: knowledge\ntriggers: [x]\npriority: 1\n"
           "---\nhello\n")
    agents = ma.load_microagents()
    matched = ma.match("x", agents)
    rendered = ma.render_injection(matched)
    assert '<microagent name="a" type="knowledge">' in rendered
    assert "hello" in rendered
    assert "</microagent>" in rendered


def test_render_injection_empty_matches():
    assert ma.render_injection([]) == ""


def test_match_empty_text_returns_empty(micro_dir):
    _write(micro_dir, "a.md",
           "---\nname: a\ntype: knowledge\ntriggers: [x]\npriority: 1\n"
           "---\nbody\n")
    agents = ma.load_microagents()
    assert ma.match("", agents) == []


def test_repo_type_loads_without_triggers(micro_dir):
    _write(micro_dir, "conv.md",
           "---\nname: conventions\ntype: repo\npriority: 1\n"
           "---\nUse pytest. Run `make test`.\n")
    agents = ma.load_microagents()
    assert len(agents) == 1
    assert agents[0].type == "repo"
    assert agents[0].triggers == ()


def test_repo_type_always_matches(micro_dir):
    _write(micro_dir, "conv.md",
           "---\nname: conventions\ntype: repo\npriority: 1\n"
           "---\nrepo body\n")
    _write(micro_dir, "trig.md",
           "---\nname: pytips\ntype: knowledge\ntriggers: [pytest]\npriority: 1\n"
           "---\npytest body\n")
    agents = ma.load_microagents()
    # No keyword in text — only repo type should match
    hits = ma.match("totally unrelated text", agents)
    assert len(hits) == 1
    assert hits[0].name == "conventions"
    # With keyword — both fire, repo first if higher priority (tie → name order)
    hits = ma.match("pytest discussion", agents)
    assert {h.name for h in hits} == {"conventions", "pytips"}
