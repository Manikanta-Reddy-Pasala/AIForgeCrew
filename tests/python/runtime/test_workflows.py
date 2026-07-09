"""Workflow registry — WORKFLOW.md parse/search/author + folder creation."""
import os
import pytest


@pytest.fixture
def wf(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setenv("AIFORGE_WORKFLOWS_DIR", str(tmp_path / "workflows"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "m.db"))
    import importlib
    from aiforge_core.runtime import workflows
    importlib.reload(workflows)
    return workflows


def test_ensure_dirs_creates_both(wf, tmp_path):
    out = wf.ensure_dirs()
    assert (tmp_path / "skills").is_dir() and (tmp_path / "workflows").is_dir()
    assert "skills" in out and "workflows" in out


def test_write_and_search(wf):
    r = wf.write_workflow("Release cut", "ship a release",
                          "## Steps\n1. tag\n2. push", triggers=["release", "deploy"])
    assert r["ok"] and r["path"].endswith("WORKFLOW.md")
    hits = wf.search("how do we cut a release")
    assert hits and hits[0]["name"] == "Release cut"


def test_write_requires_name_and_body(wf):
    assert wf.write_workflow("", "d", "b")["error"] == "name and body are required"
    assert wf.write_workflow("n", "d", "")["error"] == "name and body are required"


def test_repo_scope_writes_under_repo(wf, tmp_path, monkeypatch):
    repo = tmp_path / "repo"; repo.mkdir()
    monkeypatch.setenv("AIFORGE_WORKSPACE_DIR", str(repo))
    r = wf.write_workflow("Repo flow", "x", "## body", scope="repo", cwd=str(repo))
    assert r["ok"] and "/.aiforge/workflows/" in r["path"]


def test_handwritten_workflow_picked_up(wf):
    d = wf._global_dir() / "manual"
    d.mkdir(parents=True)
    (d / "WORKFLOW.md").write_text(
        "---\nname: Manual flow\ndescription: dropped by hand\ntriggers: [onboard]\n---\n\n## steps\ndo it")
    hits = wf.search("onboard")
    assert any(h["name"] == "Manual flow" for h in hits)


def test_auto_context_surfaces_relevant(wf):
    wf.write_workflow("Release cut", "ship a release",
                      "## Steps\n1. tag\n2. push", triggers=["release", "deploy"])
    block = wf.auto_context("how do we cut a release")
    assert "APPLICABLE WORKFLOWS" in block and "Release cut" in block
    assert wf.auto_context("unrelated quantum chromodynamics") == ""


def test_library_ships_builtin_flow(wf, monkeypatch, tmp_path):
    from aiforge_core.runtime import skills as sk
    # The library ships a small set of PORTABLE built-in playbooks (the
    # jira/confluence read/write flow + the ticket→MR workflow) — no user data,
    # no product/repo names. User skills/workflows override a builtin by name.
    res = wf.ensure_dirs()
    assert (tmp_path / "skills").is_dir() and (tmp_path / "workflows").is_dir()
    assert "skills" in res and "workflows" in res
    skn = {s.name for s in sk.load()}
    wfn = {w.name for w in wf.load()}
    assert {"jira-read", "confluence-read", "jira-write"} <= skn
    assert "jira-ticket-to-mr" in wfn
    # Builtins live in the package, NOT copied into the user-only global dir.
    assert list(wf._global_dir().glob("*.md")) == []
    # idempotent: second call is a no-op migration (nothing seeded to remove)
    assert wf.ensure_dirs()["workflows"]["removed_seeded"] == 0


def test_write_with_scripts_creates_scripts_folder(wf):
    r = wf.write_workflow(
        "Nightly export", "export the data", "## Steps\n1. run scripts/export.sh",
        triggers=["export"],
        scripts=[{"name": "export.sh", "content": "#!/usr/bin/env bash\necho ok\n"},
                 {"name": "verify.py", "content": "print('ok')\n"}])
    assert r["ok"], r
    paths = r["scripts"]
    assert len(paths) == 2
    for p in paths:
        assert "/scripts/" in p and os.path.isfile(p)
        assert os.access(p, os.X_OK)          # chmod +x applied
    # runtime surfaces: auto_context tells the agent to RUN the scripts…
    block = wf.auto_context("nightly export")
    assert "helper scripts" in block and "export.sh" in block
    # …and search hits carry the script paths.
    hit = next(h for h in wf.search("nightly export") if h["name"] == "Nightly export")
    assert any(p.endswith("export.sh") for p in hit["scripts"])


def test_write_scripts_dict_form_accepted(wf):
    r = wf.write_workflow("Dict flow", "x", "## body",
                          scripts={"go.sh": "#!/usr/bin/env bash\necho hi\n"})
    assert r["ok"] and r["scripts"][0].endswith("go.sh")


def test_script_syntax_error_aborts_whole_write(wf):
    r = wf.write_workflow("Broken flow", "x", "## body",
                          scripts=[{"name": "bad.sh",
                                    "content": "if [ ; then\nfi\n"}])
    assert not r["ok"] and "syntax" in r["error"]
    # nothing saved — the workflow must not exist half-written
    assert not any(w.name == "Broken flow" for w in wf.load())
    r2 = wf.write_workflow("Broken py", "x", "## body",
                           scripts=[{"name": "bad.py", "content": "def f(:\n"}])
    assert not r2["ok"] and "syntax" in r2["error"]


def test_script_name_traversal_rejected(wf):
    for bad in ("../evil.sh", "a/b.sh", "/etc/x.sh", ".hidden.sh", ""):
        r = wf.write_workflow("Evil", "x", "## body",
                              scripts=[{"name": bad, "content": "echo hi"}])
        assert not r["ok"], bad


def test_delete_removes_scripts_folder(wf):
    r = wf.write_workflow("Temp flow", "x", "## body",
                          scripts=[{"name": "t.sh", "content": "echo hi\n"}])
    assert r["ok"]
    wf_dir = os.path.dirname(r["path"])
    assert os.path.isdir(os.path.join(wf_dir, "scripts"))
    d = wf.delete_workflow("Temp flow")
    assert d["ok"]
    assert not os.path.exists(wf_dir)         # slug dir incl. scripts/ gone


def test_hard_gate_runs_scripts_and_refuses_failures(wf):
    """HARD gate (job-builder parity): the script is actually RUN — a failing
    run refuses the whole save with the output; nothing is written."""
    r = wf.write_workflow("Failing flow", "x", "## body",
                          scripts=[{"name": "boom.sh",
                                    "content": "echo doomed >&2\nexit 3\n"}])
    assert not r["ok"] and "FAILED its test run" in r["error"]
    assert "doomed" in r["error"]              # output surfaced for the fix
    assert not any(w.name == "Failing flow" for w in wf.load())


def test_hard_gate_honours_custom_test_command(wf):
    # script would FAIL bare (exit 2 without --dry-run) but its declared test
    # command passes → saved.
    content = ('#!/usr/bin/env bash\nif [ "$1" = "--dry-run" ]; then\n'
               '  echo dry\n  exit 0\nfi\nexit 2\n')
    r = wf.write_workflow("Dry flow", "x", "## body",
                          scripts=[{"name": "d.sh", "content": content,
                                    "test": "bash d.sh --dry-run"}])
    assert r["ok"], r
    r2 = wf.write_workflow("Dry flow 2", "x", "## body",
                           scripts=[{"name": "d.sh", "content": content}])
    assert not r2["ok"]                        # bare run exits 2 → refused


def test_hard_gate_skip_optout(wf):
    r = wf.write_workflow("Prod-only flow", "x", "## body",
                          scripts=[{"name": "prod.sh",
                                    "content": "exit 7\n", "test": "skip"}])
    assert r["ok"], r


def test_learn_workflow_passes_hard_gate(wf, monkeypatch):
    monkeypatch.setenv("AIFORGE_BUILDER_ELABORATE", "0")
    from aiforge_core.runtime import chat_agent as ca
    ok = ca._t_learn_workflow({"name": "Gated", "description": "x",
                               "body": "## steps",
                               "scripts": [{"name": "s.sh",
                                            "content": "echo hi\n"}]},
                              cwd=None)
    assert ok["ok"] and ok["scripts"][0].endswith("s.sh")
    bad = ca._t_learn_workflow({"name": "Gated bad", "description": "x",
                                "body": "## steps",
                                "scripts": [{"name": "b.sh",
                                             "content": "exit 1\n"}]},
                               cwd=None)
    assert not bad["ok"] and "FAILED" in bad["error"]


def test_search_fuzzy_inflection_and_typo(wf):
    """A QUESTION that doesn't use the exact trigger words still finds the
    workflow: inflection (deployment≈deploy) and a close typo (releese≈release)
    both score via the fuzzy overlap in the shared scorer."""
    wf.write_workflow("Release cut", "ship a release",
                      "## Steps\n1. tag\n2. push", triggers=["release", "deploy"])
    assert any(h["name"] == "Release cut"
               for h in wf.search("what is our deployment procedure?"))
    assert any(h["name"] == "Release cut"
               for h in wf.search("how do we cut a releese"))


def test_migration_removes_stale_seeded_copies(wf, tmp_path):
    # A prior version seeded builtins (marked `source: builtin`) into the global
    # dir; the v2 migration in ensure_dirs removes those stale copies ONCE while
    # leaving user-authored playbooks untouched.
    gdir = wf._global_dir()
    gdir.mkdir(parents=True, exist_ok=True)
    seeded = gdir / "ship-a-feature.md"
    seeded.write_text("---\nname: ship-a-feature\nsource: builtin\n---\n\nold copy\n")
    mine = gdir / "my-flow.md"
    mine.write_text("---\nname: my-flow\ndescription: mine\n---\n\nkeep me\n")

    res = wf.ensure_dirs()
    assert res["workflows"]["removed_seeded"] >= 1
    assert not seeded.exists()             # stale builtin copy removed
    assert mine.exists()                   # user file untouched

    # Migration runs once: re-dropping a seeded copy is NOT re-removed, and the
    # user file still survives.
    seeded.write_text("---\nname: ship-a-feature\nsource: builtin\n---\n\nold copy\n")
    assert wf.ensure_dirs()["workflows"]["removed_seeded"] == 0
    assert seeded.exists() and mine.exists()
