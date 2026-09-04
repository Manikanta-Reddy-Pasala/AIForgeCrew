"""The playbook library: skills, workflows and rules.

One classification carries the UI here. The bundled defaults are COPIED into
the user-writable global dir at startup, keeping their filenames, so a path
check cannot tell a seeded default from something the user wrote — the
filename is the only signal left, and it is what "default" vs "custom" is
decided on. Getting that wrong would either hide a user's own playbook behind
a "default" badge or offer to reset one that never shipped.

The rest is a small CRUD surface over three registries, where an unknown kind
is a 404 and a rejected write is a 400 carrying the registry's own reason.
"""
from __future__ import annotations

import types as pytypes

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import library as lib


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(lib.router)
    return TestClient(app)


def _skill(name="deploy", source="/global/deploy.md", **kw):
    base = {"name": name, "description": "how to deploy", "triggers": ["ship"],
            "body": "1. build\n2. ship", "source": source, "always": False}
    base.update(kw)
    return pytypes.SimpleNamespace(**base)


_REAL_BUNDLED = lib._bundled_names


@pytest.fixture(autouse=True)
def bundled(monkeypatch):
    """A known set of shipped playbook filenames."""
    _REAL_BUNDLED.__dict__.pop("_cache", None)
    monkeypatch.setattr(lib, "_bundled_names",
                        lambda kind: {"deploy.md"} if kind == "skills" else set())
    yield
    _REAL_BUNDLED.__dict__.pop("_cache", None)


@pytest.fixture
def real_bundled(monkeypatch):
    """Undo the stub so the on-disk scan itself is exercised."""
    monkeypatch.setattr(lib, "_bundled_names", _REAL_BUNDLED)
    _REAL_BUNDLED.__dict__.pop("_cache", None)
    return _REAL_BUNDLED


# ─── default vs custom ─────────────────────────────────────────────────


def test_a_seeded_default_is_recognised_by_its_filename(bundled):
    """ensure_dirs copies the bundled files into the user dir, so the path
    says nothing — only the name does."""
    assert lib._library_origin("/home/u/.aiforge/skills/deploy.md", "skills") \
        == "default"


def test_a_user_authored_playbook_is_custom(bundled):
    assert lib._library_origin("/home/u/.aiforge/skills/mine.md", "skills") \
        == "custom"


def test_an_unclassifiable_source_is_custom(bundled):
    assert lib._library_origin("", "skills") == "custom"


def test_the_bundled_list_is_read_from_disk_once(real_bundled, monkeypatch,
                                                 tmp_path):
    import aiforge_core.runtime.workflows as wf
    pkg = tmp_path / "runtime"
    (pkg / "builtin_playbooks" / "skills").mkdir(parents=True)
    (pkg / "builtin_playbooks" / "skills" / "deploy.md").write_text("x")
    monkeypatch.setattr(wf, "__file__", str(pkg / "workflows.py"))
    assert lib._bundled_names("skills") == {"deploy.md"}
    # cached: a later miss on disk still answers
    assert lib._bundled_names("skills") == {"deploy.md"}


def test_a_kind_with_no_bundled_dir_has_no_defaults(real_bundled, monkeypatch,
                                                    tmp_path):
    import aiforge_core.runtime.workflows as wf
    monkeypatch.setattr(wf, "__file__", str(tmp_path / "workflows.py"))
    assert lib._bundled_names("skills") == set()


def test_an_unreadable_package_yields_no_defaults(real_bundled, monkeypatch):
    import aiforge_core.runtime.workflows as wf
    monkeypatch.delattr(wf, "__file__", raising=False)
    assert lib._bundled_names("skills") == set()


# ─── listing ───────────────────────────────────────────────────────────


def test_skills_are_listed_with_their_origin(client, monkeypatch):
    from aiforge_core.runtime import skills
    monkeypatch.setattr(skills, "load",
                        lambda: [_skill(), _skill("mine", "/global/mine.md")])
    rows = client.get("/api/library/skills").json()
    assert [r["origin"] for r in rows] == ["default", "custom"]
    assert rows[0]["triggers"] == ["ship"]
    assert rows[0]["body"].startswith("1.")


def test_workflows_are_listed(client, monkeypatch):
    from aiforge_core.runtime import workflows
    monkeypatch.setattr(workflows, "load", lambda: [_skill("release")])
    rows = client.get("/api/library/workflows").json()
    assert rows[0]["name"] == "release"
    assert rows[0]["origin"] == "custom"


def test_rules_carry_their_globs_and_scope(client, monkeypatch):
    from aiforge_core.runtime import repo_rules
    rule = pytypes.SimpleNamespace(name="python-style", description="pep8",
                                   triggers=["py"], scope="repo", body="rules",
                                   source="/global/python-style.md",
                                   globs=["**/*.py"], always=True)
    monkeypatch.setattr(repo_rules, "load_global_and_builtin", lambda: [rule])
    row = client.get("/api/library/rules").json()[0]
    assert row["globs"] == ["**/*.py"]
    assert row["scope"] == "repo"
    assert row["always"] is True


def test_an_unknown_kind_is_a_404(client):
    assert client.get("/api/library/nonsense").status_code == 404


def test_the_public_workflow_registry_is_served(client, monkeypatch):
    import aiforge_core.workflows as wfpkg
    entry = pytypes.SimpleNamespace(
        to_public_dict=lambda: {"id": "wf-1", "name": "Release"})
    monkeypatch.setattr(wfpkg, "list_all", lambda: [entry])
    assert client.get("/api/workflows").json() == [{"id": "wf-1",
                                                    "name": "Release"}]


# ─── creating ──────────────────────────────────────────────────────────


@pytest.fixture
def writers(monkeypatch):
    from aiforge_core.runtime import repo_rules, skills, workflows
    seen: dict = {"result": {"ok": True, "path": "/global/x.md"}}
    monkeypatch.setattr(skills, "write_skill",
                        lambda name, desc, body, triggers: seen.update(
                            kind="skills", name=name, triggers=triggers)
                        or seen["result"])
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda name, desc, body, triggers: seen.update(
                            kind="workflows", name=name) or seen["result"])
    monkeypatch.setattr(repo_rules, "write_rule",
                        lambda name, body, globs=None, always=True:
                        seen.update(kind="rules", globs=globs, always=always)
                        or seen["result"])
    return seen


@pytest.mark.parametrize("kind", ["skills", "workflows", "rules"])
def test_each_kind_is_written_to_its_registry(client, writers, kind):
    r = client.post(f"/api/library/{kind}",
                    json={"name": "n", "body": "b", "description": "d"})
    assert r.status_code == 201
    assert writers["kind"] == kind


def test_a_comma_string_of_triggers_is_split(client, writers):
    client.post("/api/library/skills",
                json={"name": "n", "body": "b", "triggers": "a, b ,, c"})
    assert writers["triggers"] == ["a", "b", "c"]


def test_a_rules_globs_and_always_flag_are_forwarded(client, writers):
    client.post("/api/library/rules",
                json={"name": "n", "body": "b", "globs": ["**/*.py"],
                      "always": False})
    assert writers["globs"] == ["**/*.py"]
    assert writers["always"] is False


@pytest.mark.parametrize("payload", [{"body": "b"}, {"name": "n"},
                                     {"name": " ", "body": "b"}])
def test_a_name_and_body_are_required(client, writers, payload):
    r = client.post("/api/library/skills", json=payload)
    assert r.status_code == 400
    assert "name and body" in r.json()["detail"]


def test_a_rejected_write_carries_the_registrys_reason(client, writers):
    writers["result"] = {"ok": False, "error": "script test failed"}
    r = client.post("/api/library/workflows", json={"name": "n", "body": "b"})
    assert r.status_code == 400
    assert r.json()["detail"] == "script test failed"


def test_creating_an_unknown_kind_is_a_404(client, writers):
    assert client.post("/api/library/nonsense",
                       json={"name": "n", "body": "b"}).status_code == 404


# ─── deleting + clearing ───────────────────────────────────────────────


@pytest.fixture
def deleters(monkeypatch):
    from aiforge_core.runtime import repo_rules, skills, workflows
    seen: dict = {"result": {"ok": True, "deleted": "n"}}
    for mod, fn, kind in ((skills, "delete_skill", "skills"),
                          (workflows, "delete_workflow", "workflows"),
                          (repo_rules, "delete_rule", "rules")):
        monkeypatch.setattr(mod, fn,
                            lambda name, _k=kind: seen.update(kind=_k, name=name)
                            or seen["result"])
    for mod, fn, kind in ((skills, "clear_skills", "skills"),
                          (workflows, "clear_workflows", "workflows"),
                          (repo_rules, "clear_rules", "rules")):
        monkeypatch.setattr(mod, fn,
                            lambda _k=kind: {"ok": True, "cleared": _k})
    return seen


@pytest.mark.parametrize("kind", ["skills", "workflows", "rules"])
def test_one_item_is_deleted_from_its_registry(client, deleters, kind):
    assert client.delete(f"/api/library/{kind}/thing").status_code == 200
    assert deleters == {**deleters, "kind": kind, "name": "thing"}


def test_deleting_something_that_is_not_there_is_a_404(client, deleters):
    deleters["result"] = {"ok": False, "error": "no such skill"}
    r = client.delete("/api/library/skills/ghost")
    assert r.status_code == 404
    assert r.json()["detail"] == "no such skill"


def test_deleting_an_unknown_kind_is_a_404(client, deleters):
    assert client.delete("/api/library/nonsense/x").status_code == 404


@pytest.mark.parametrize("kind", ["skills", "workflows", "rules"])
def test_a_whole_kind_can_be_cleared(client, deleters, kind):
    assert client.delete(f"/api/library/{kind}").json() == {"ok": True,
                                                            "cleared": kind}


def test_clearing_an_unknown_kind_is_a_404(client, deleters):
    assert client.delete("/api/library/nonsense").status_code == 404


# ─── drafting with the model ───────────────────────────────────────────


@pytest.fixture
def drafter(monkeypatch):
    from aiforge_core.llm import client as llm
    seen: dict = {"draft": "# Draft"}

    def _complete(role, convo, max_tokens=None):
        seen.update(role=role, user=convo[1]["content"], max_tokens=max_tokens)
        if isinstance(seen["draft"], Exception):
            raise seen["draft"]
        return seen["draft"]
    monkeypatch.setattr(llm, "complete", _complete)
    return seen


@pytest.mark.parametrize("kind,marker", [("skills", "SKILL.md"),
                                         ("workflows", "WORKFLOW.md"),
                                         ("rules", "coding RULE")])
def test_each_kind_gets_its_own_authoring_prompt(client, drafter, kind, marker):
    body = client.post(f"/api/library/{kind}/generate",
                       json={"prompt": "deploying to qa"}).json()
    assert body == {"ok": True, "draft": "# Draft"}
    assert marker in drafter["user"]
    assert "deploying to qa" in drafter["user"]


def test_the_drafting_role_can_be_chosen(client, drafter):
    client.post("/api/library/skills/generate",
                json={"prompt": "x", "role": "planner"})
    assert drafter["role"] == "planner"


def test_the_default_drafting_role(client, drafter):
    client.post("/api/library/skills/generate", json={"prompt": "x"})
    assert drafter["role"] == "architect"


def test_a_prompt_is_required(client, drafter):
    r = client.post("/api/library/skills/generate", json={"prompt": "  "})
    assert r.status_code == 400
    assert "prompt is required" in r.json()["detail"]


def test_generating_an_unknown_kind_is_a_404(client, drafter):
    assert client.post("/api/library/nonsense/generate",
                       json={"prompt": "x"}).status_code == 404


def test_a_model_or_credit_error_is_surfaced_as_a_502(client, drafter):
    drafter["draft"] = RuntimeError("insufficient credit")
    r = client.post("/api/library/skills/generate", json={"prompt": "x"})
    assert r.status_code == 502
    assert "insufficient credit" in r.json()["detail"]
