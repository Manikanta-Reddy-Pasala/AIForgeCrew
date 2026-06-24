"""Skill registry: SKILL.md standard, relevance search, self-authoring,
memory linkage, and chat-tool wiring."""
from __future__ import annotations

import importlib

import pytest

from aiforge_core.runtime import skills


def _seed_embedded_memory(tmp_path, monkeypatch):
    for k in ("AIFORGE_MEMORY_BACKEND", "NEO4J_URI", "AIFORGE_NEO4J_URI",
              "AIFORGE_PG_URL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "mem.db"))
    import aiforge_core.memory.backend_select as bs; importlib.reload(bs)
    import aiforge_core.memory.sqlite_memory as sm; importlib.reload(sm)
    return sm


# ─── SKILL.md standard loading ─────────────────────────────────────────

def test_loads_skill_md_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_SKILLS_DIR", str(tmp_path / "gskills"))
    d = tmp_path / "gskills" / "flyway-migrations"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: flyway-migrations\ndescription: how to add a DB migration\n"
        "triggers: [migration, flyway, schema]\n---\nPut .sql under db/migration; "
        "never use ddl-auto.")
    loaded = skills.load(str(tmp_path / "norepo"))
    names = [s.name for s in loaded]
    assert "flyway-migrations" in names
    sk = next(s for s in loaded if s.name == "flyway-migrations")
    assert "ddl-auto" in sk.body
    assert "migration" in sk.triggers


# ─── relevance search ──────────────────────────────────────────────────

def test_search_ranks_by_relevance(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_SKILLS_DIR", str(tmp_path / "g"))
    base = tmp_path / "g"
    for n, desc, trig in [
        ("flyway-migrations", "add a DB migration", "migration, flyway"),
        ("react-component", "scaffold a React component", "react, component, tsx"),
    ]:
        sd = base / n; sd.mkdir(parents=True)
        (sd / "SKILL.md").write_text(
            f"---\nname: {n}\ndescription: {desc}\ntriggers: [{trig}]\n---\nbody for {n}")
    hits = skills.search("I need to add a flyway migration", str(tmp_path / "x"), k=5)
    assert hits and hits[0]["name"] == "flyway-migrations"
    assert all(h["score"] > 0 for h in hits)


def test_auto_context_includes_always_and_relevant(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_SKILLS_DIR", str(tmp_path / "g"))
    base = tmp_path / "g"
    al = base / "house-style"; al.mkdir(parents=True)
    (al / "SKILL.md").write_text(
        "---\nname: house-style\nalways: true\n---\nUse 2-space indent everywhere.")
    rel = base / "docker"; rel.mkdir(parents=True)
    (rel / "SKILL.md").write_text(
        "---\nname: docker\ntriggers: [docker, container]\n---\nUse multi-stage builds.")
    ctx = skills.auto_context("how do I build the docker image", str(tmp_path / "x"))
    assert "house-style" in ctx          # always-on
    assert "docker" in ctx               # relevant
    # unrelated query → still gets always-on, not the docker skill body
    ctx2 = skills.auto_context("rename a variable", str(tmp_path / "x"))
    assert "house-style" in ctx2
    assert "multi-stage" not in ctx2


# ─── self-authoring + memory linkage ──────────────────────────────────

def test_write_skill_creates_file_and_memory(tmp_path, monkeypatch):
    sm = _seed_embedded_memory(tmp_path, monkeypatch)
    monkeypatch.setenv("AIFORGE_SKILLS_DIR", str(tmp_path / "g"))
    res = skills.write_skill(
        name="reset NATS consumer",
        description="when the durable consumer is stuck",
        body="1. delete the consumer\n2. recreate with the same name\n3. restart pod",
        triggers=["nats", "consumer", "stuck"],
        cwd=str(tmp_path / "repo"), scope="global")
    assert res["ok"] is True
    from pathlib import Path
    assert Path(res["path"]).is_file()
    assert res.get("memory") is True
    assert sm.stats()["total"] == 1            # learning recorded in memory
    # the authored skill is now discoverable by search
    hits = skills.search("nats consumer stuck", str(tmp_path / "repo"))
    assert any(h["name"] == "reset NATS consumer" for h in hits)


def test_write_skill_requires_name_and_body(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_SKILLS_DIR", str(tmp_path / "g"))
    assert skills.write_skill("", "d", "b")["ok"] is False
    assert skills.write_skill("n", "d", "")["ok"] is False


def test_repo_scope_writes_into_repo(tmp_path, monkeypatch):
    monkeypatch.delenv("AIFORGE_WORKSPACE_DIR", raising=False)
    monkeypatch.setenv("AIFORGE_SKILLS_DIR", str(tmp_path / "g"))
    res = skills.write_skill("x", "d", "body", cwd=str(tmp_path / "myrepo"), scope="repo")
    assert res["ok"] and "/myrepo/.aiforge/skills/" in res["path"]


# ─── chat tool wiring ──────────────────────────────────────────────────

def test_chat_skill_tools_registered():
    from aiforge_core.runtime import chat_agent as ca
    assert "skill_search" in ca.TOOLS
    assert "learn_skill" in ca.TOOLS
    assert "skill_search" in ca._READONLY_TOOLS   # safe in plan mode


def test_chat_learn_skill_tool(tmp_path, monkeypatch):
    _seed_embedded_memory(tmp_path, monkeypatch)
    monkeypatch.setenv("AIFORGE_SKILLS_DIR", str(tmp_path / "g"))
    from aiforge_core.runtime import chat_agent as ca
    out = ca._t_learn_skill(
        {"name": "deploy hotfix", "description": "fast prod patch",
         "body": "tag, push, watch tekton", "triggers": "deploy,hotfix"},
        str(tmp_path / "repo"))
    assert out["ok"] is True
    found = ca._t_skill_search({"query": "deploy hotfix"}, str(tmp_path / "repo"))
    assert found["ok"] and any(s["name"] == "deploy hotfix" for s in found["skills"])


def test_doer_skill_tools_exported():
    from aiforge_core.runtime import doer_tools as dt
    assert hasattr(dt, "skill_search") and hasattr(dt, "learn_skill")


def test_md_store_bullet_dedup_is_line_based(tmp_path, monkeypatch):
    # audit fix: a short bullet that's a SUBSTRING of an existing longer one
    # must NOT be treated as a duplicate.
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import importlib
    from aiforge_core.memory import md_store
    importlib.reload(md_store)
    md_store.append_bullet(source="rules:x", title="x", bullet="use yarn always")
    md_store.append_bullet(source="rules:x", title="x", bullet="use yarn")  # substring
    p = md_store._find_by_source("rules:x")
    body = md_store._parse(p)["body"]
    assert "- use yarn always" in body
    assert "- use yarn\n" in body + "\n"        # the short one was NOT dropped
    # exact same line IS deduped
    md_store.append_bullet(source="rules:x", title="x", bullet="use yarn")
    assert md_store._parse(md_store._find_by_source("rules:x"))["body"].count("- use yarn\n") <= 1
