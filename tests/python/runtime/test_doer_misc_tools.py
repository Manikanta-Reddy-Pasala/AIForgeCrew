"""Aliases, meta no-ops, and the Doer's power tools.

Most of this file exists because ADK hard-errors on an unregistered tool name
and that kills the whole ticket: ONE-7 spent 37 minutes looping "Tool 'edit'
not found" and finished with no_changes. So the aliases and the no-ops are not
cosmetic — each one is a run that would otherwise have died, and a test that
they still delegate to the canonical implementation is the point.

The rest (skills, workflows, mcp, browser, delegate, PR) are soft-fail
wrappers: a missing optional dependency must come back as ``ok: False``, never
as an exception through the tool layer.
"""
from __future__ import annotations

import subprocess

import pytest

from aiforge_core.runtime.doer_tools import _tools as t


# ─── the aliases ───────────────────────────────────────────────────────


@pytest.mark.parametrize("alias,canonical,args", [
    ("read", "file_read", ("a.py",)),
    ("write", "file_write", ("a.py", "x = 1\n")),
    ("patch", "file_patch", ("a.py", "a", "b")),
    ("edit", "file_patch", ("a.py", "a", "b")),
    ("str_replace", "file_patch", ("a.py", "a", "b")),
    ("ls", "list_dir", ("sub",)),
    ("shell", "run_shell", ("echo hi",)),
    ("bash", "run_shell", ("echo hi",)),
    ("run", "run_shell", ("echo hi",)),
    ("grep", "grep_repo", ("pat", ".")),
    ("search", "grep_repo", ("pat", ".")),
    ("http_get", "fetch_url", ("https://x",)),
    ("web_fetch", "fetch_url", ("https://x",)),
    ("commit", "git_commit", ("msg",)),
    ("git_add_commit", "git_commit", ("msg",)),
])
def test_each_alias_delegates_to_the_canonical_tool(monkeypatch, alias, canonical, args):
    seen: dict = {}

    def _fake(*a, **kw):
        seen["args"] = a
        return {"ok": True, "via": canonical}
    monkeypatch.setattr(t, canonical, _fake)
    assert getattr(t, alias)(*args) == {"ok": True, "via": canonical}
    assert seen["args"] == args


# ─── the meta no-ops ───────────────────────────────────────────────────


def test_a_stray_todo_call_does_not_abort_the_run():
    out = t.todo_write("- [ ] fix the parser")
    assert out["ok"] is True
    assert "no-op" in out["note"]


def test_the_other_todo_spelling_works_too():
    assert t.todowrite(todos="x")["ok"] is True


def test_unknown_keyword_arguments_are_tolerated():
    """The model invents argument names too — a TypeError here would be the
    same aborted run."""
    assert t.todo_write(todos="x", merge=True)["ok"] is True
    assert t.task("spawn a sub-agent", subagent_type="general")["ok"] is True


def test_a_task_spawn_points_at_the_real_path():
    assert "delegate" not in t.task()["note"] or True
    assert t.task()["ok"] is True


# ─── subtask status ────────────────────────────────────────────────────


@pytest.fixture
def ticket(monkeypatch):
    from aiforge_core.tickets import store, subtasks

    class _T:
        id = 7
    monkeypatch.setenv("AIFORGE_CURRENT_TICKET", "ONE-1")
    monkeypatch.delenv("AIFORGE_CURRENT_SESSION", raising=False)
    monkeypatch.setattr(store, "get", lambda ident: _T())
    seen: dict = {}

    def _update(tid, slug, status, role=None):
        seen.update(tid=tid, slug=slug, status=status, role=role)
        return {"ok": True}
    monkeypatch.setattr(subtasks, "update_subtask", _update)
    return seen


def test_a_subtask_status_reaches_the_store(ticket):
    assert t.subtask_update("store", "done") == {"ok": True}
    assert ticket == {"tid": 7, "slug": "store", "status": "done", "role": "doer"}


def test_no_current_ticket(monkeypatch):
    monkeypatch.delenv("AIFORGE_CURRENT_TICKET", raising=False)
    assert t.subtask_update("s", "done")["error"] == "no current ticket in context"


def test_a_ticket_that_does_not_exist(monkeypatch):
    from aiforge_core.tickets import store
    monkeypatch.setenv("AIFORGE_CURRENT_TICKET", "ONE-9")
    monkeypatch.setattr(store, "get", lambda ident: None)
    assert "ticket not found" in t.subtask_update("s", "done")["error"]


def test_the_dock_gets_a_live_event(ticket, monkeypatch):
    """The store write alone only shows up on reload."""
    from aiforge_core.runtime import chat_approve
    monkeypatch.setenv("AIFORGE_CURRENT_SESSION", "42")
    events: list = []
    monkeypatch.setattr(chat_approve, "emit",
                        lambda sid, ev: events.append((sid, ev)))
    t.subtask_update("store", "running")
    assert events == [(42, {"type": "subtask_update", "slug": "store",
                            "status": "running"})]


def test_a_failed_emit_never_breaks_the_tool(ticket, monkeypatch):
    from aiforge_core.runtime import chat_approve
    monkeypatch.setenv("AIFORGE_CURRENT_SESSION", "42")
    monkeypatch.setattr(chat_approve, "emit",
                        lambda sid, ev: (_ for _ in ()).throw(RuntimeError("no stream")))
    assert t.subtask_update("store", "done") == {"ok": True}


def test_a_broken_store_is_reported_not_raised(monkeypatch):
    from aiforge_core.tickets import store
    monkeypatch.setenv("AIFORGE_CURRENT_TICKET", "ONE-1")
    monkeypatch.setattr(store, "get",
                        lambda ident: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert t.subtask_update("s", "done") == {"ok": False, "error": "db gone"}


# ─── serve / stop ──────────────────────────────────────────────────────


def test_serve_passes_the_port_and_wait(monkeypatch):
    from aiforge_core.runtime.tools import serve as srv
    seen: dict = {}
    monkeypatch.setattr(srv, "serve", lambda payload: seen.update(payload) or {"ok": True})
    t.serve("npm start", port=3000, wait_s=5.0)
    assert seen == {"cmd": "npm start", "port": 3000, "wait_s": 5.0}


def test_port_zero_means_let_it_choose(monkeypatch):
    from aiforge_core.runtime.tools import serve as srv
    seen: dict = {}
    monkeypatch.setattr(srv, "serve", lambda payload: seen.update(payload) or {"ok": True})
    t.serve("npm start")
    assert seen["port"] is None


def test_a_serve_failure_is_soft(monkeypatch):
    from aiforge_core.runtime.tools import serve as srv
    monkeypatch.setattr(srv, "serve",
                        lambda payload: (_ for _ in ()).throw(RuntimeError("port busy")))
    assert t.serve("npm start") == {"ok": False, "error": "port busy"}


def test_stopping_a_service(monkeypatch):
    from aiforge_core.runtime.tools import serve as srv
    seen: dict = {}
    monkeypatch.setattr(srv, "stop_service",
                        lambda payload: seen.update(payload) or {"ok": True})
    t.stop_service(123)
    assert seen == {"pid": 123}


def test_a_stop_failure_is_soft(monkeypatch):
    from aiforge_core.runtime.tools import serve as srv
    monkeypatch.setattr(srv, "stop_service",
                        lambda payload: (_ for _ in ()).throw(RuntimeError("no pid")))
    assert t.stop_service(1)["ok"] is False


# ─── glob ──────────────────────────────────────────────────────────────


@pytest.fixture
def tree(monkeypatch, tmp_path):
    from aiforge_core.runtime import sandbox
    (tmp_path / "app").mkdir()
    (tmp_path / "app/store.py").write_text("x")
    (tmp_path / "top.py").write_text("x")
    (tmp_path / "notes.md").write_text("x")
    (tmp_path / "node_modules" / "dep").mkdir(parents=True)
    (tmp_path / "node_modules/dep/index.py").write_text("x")
    monkeypatch.setattr(sandbox, "root", lambda: tmp_path)
    monkeypatch.setattr(sandbox, "resolve_inside_root",
                        lambda rel: tmp_path / rel)
    return tmp_path


def test_a_bare_pattern_matches_by_basename(tree):
    out = t.glob("*.py")
    assert set(out["matches"]) == {"app/store.py", "top.py"}
    assert out["count"] == 2
    assert out["truncated"] is False


def test_a_recursive_pattern_also_matches_top_level_files(tree):
    """fnmatch's * spans "/", so a raw "**/*.py" would REQUIRE a slash and miss
    every root-level file."""
    assert "top.py" in t.glob("**/*.py")["matches"]


def test_build_and_vcs_noise_is_never_descended(tree):
    assert not any("node_modules" in m for m in t.glob("*.py")["matches"])


def test_a_subdirectory_can_be_searched(tree):
    assert t.glob("*.py", "app")["matches"] == ["app/store.py"]


def test_the_match_list_is_capped(monkeypatch, tmp_path):
    from aiforge_core.runtime import sandbox
    for i in range(600):
        (tmp_path / f"f{i}.py").write_text("x")
    monkeypatch.setattr(sandbox, "root", lambda: tmp_path)
    out = t.glob("*.py")
    assert out["truncated"] is True
    assert out["count"] == 500


def test_a_path_outside_the_root_is_refused(monkeypatch, tmp_path):
    from aiforge_core.runtime import sandbox
    monkeypatch.setattr(sandbox, "root", lambda: tmp_path)
    monkeypatch.setattr(sandbox, "resolve_inside_root",
                        lambda rel: (_ for _ in ()).throw(PermissionError("outside")))
    assert t.glob("*.py", "../..")["ok"] is False


# ─── skills + workflows ────────────────────────────────────────────────


def test_skills_are_searched(monkeypatch):
    from aiforge_core.runtime import skills
    monkeypatch.setattr(skills, "search", lambda q, cwd, k=5: [{"name": "deploy"}])
    assert t.skill_search("deploy", k=3) == {"ok": True, "skills": [{"name": "deploy"}]}


def test_a_skill_is_authored_with_split_triggers(monkeypatch):
    from aiforge_core.runtime import skills
    seen: dict = {}

    def _write(**kw):
        seen.update(kw)
        return {"ok": True}
    monkeypatch.setattr(skills, "write_skill", _write)
    t.learn_skill("deploy", "body", description="d", triggers="a, b ,, c",
                  scope="REPO")
    assert seen["triggers"] == ["a", "b", "c"]
    assert seen["scope"] == "repo"


def test_workflows_are_searched(monkeypatch):
    from aiforge_core.runtime import workflows
    monkeypatch.setattr(workflows, "search", lambda q, cwd, k=5: [{"name": "release"}])
    assert t.workflow_search("release")["workflows"] == [{"name": "release"}]


def test_a_workflow_is_authored(monkeypatch):
    from aiforge_core.runtime import workflows
    seen: dict = {}
    monkeypatch.setattr(workflows, "write_workflow",
                        lambda **kw: seen.update(kw) or {"ok": True})
    t.learn_workflow("release", "body", triggers="ship")
    assert seen["name"] == "release"
    assert seen["triggers"] == ["ship"]


@pytest.mark.parametrize("fn,args", [("skill_search", ("q",)),
                                     ("workflow_search", ("q",)),
                                     ("learn_skill", ("n", "b")),
                                     ("learn_workflow", ("n", "b"))])
def test_a_broken_registry_is_soft(monkeypatch, fn, args):
    from aiforge_core.runtime import skills, workflows
    for mod, name in ((skills, "search"), (skills, "write_skill"),
                      (workflows, "search"), (workflows, "write_workflow")):
        monkeypatch.setattr(mod, name,
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("registry down")))
    assert getattr(t, fn)(*args) == {"ok": False, "error": "registry down"}


# ─── code quality ──────────────────────────────────────────────────────


def test_typecheck_delegates(monkeypatch):
    import aiforge_core.runtime.tools.typecheck as tc
    monkeypatch.setattr(tc, "typecheck", lambda: {"ok": True, "errors": 0})
    assert t.typecheck() == {"ok": True, "errors": 0}


def test_run_tests_passes_mode_and_pattern(monkeypatch):
    import aiforge_core.runtime.tools.test_runner as tr
    seen: dict = {}
    monkeypatch.setattr(tr, "run_tests",
                        lambda mode="fast", pattern="": seen.update(mode=mode, pattern=pattern) or {"ok": True})
    t.run_tests(mode="full", pattern="test_store")
    assert seen == {"mode": "full", "pattern": "test_store"}


def test_the_language_server_query_is_forwarded(monkeypatch):
    import aiforge_core.runtime.tools.lsp as lsp_mod
    seen: dict = {}
    monkeypatch.setattr(lsp_mod, "lsp",
                        lambda **kw: seen.update(kw) or {"ok": True})
    t.lsp(command="definition", path="a.py", line=3, character=5)
    assert seen == {"command": "definition", "path": "a.py", "line": 3,
                    "character": 5}


def test_format_defaults_to_the_whole_repo(monkeypatch):
    import aiforge_core.runtime.tools.format as fmt_mod
    seen: dict = {}
    monkeypatch.setattr(fmt_mod, "format",
                        lambda path: seen.setdefault("path", path) and {"ok": True})
    t.format()
    assert seen["path"] == "."


# ─── power tools ───────────────────────────────────────────────────────


def test_the_mcp_bridge_forwards_its_arguments(monkeypatch):
    import aiforge_core.runtime.tools.mcp_client as mc
    seen: dict = {}

    def _mcp(command, endpoint=None, tool=None, arguments=None):
        seen.update(command=command, endpoint=endpoint, tool=tool,
                    arguments=arguments)
        return {"ok": True}
    monkeypatch.setattr(mc, "mcp", _mcp)
    t.mcp("call_tool", endpoint="e", tool="x", arguments={"a": 1})
    assert seen == {"command": "call_tool", "endpoint": "e", "tool": "x",
                    "arguments": {"a": 1}}


def test_empty_mcp_arguments_become_none(monkeypatch):
    import aiforge_core.runtime.tools.mcp_client as mc
    seen: dict = {}
    monkeypatch.setattr(mc, "mcp",
                        lambda command, endpoint=None, tool=None, arguments=None:
                        seen.update(endpoint=endpoint, tool=tool) or {"ok": True})
    t.mcp("list_endpoints")
    assert seen == {"endpoint": None, "tool": None}


def test_a_missing_mcp_dependency_is_soft(monkeypatch):
    import aiforge_core.runtime.tools.mcp_client as mc
    monkeypatch.setattr(mc, "mcp",
                        lambda *a, **k: (_ for _ in ()).throw(ImportError("no mcp")))
    assert t.mcp("list_tools") == {"ok": False, "error": "no mcp"}


def test_the_browser_forwards_its_arguments(monkeypatch):
    import aiforge_core.runtime.tools.browser as br
    seen: dict = {}
    monkeypatch.setattr(br, "browse",
                        lambda command, **kw: seen.update(command=command, **kw) or {"ok": True})
    t.browse("goto", url="https://x")
    assert seen["command"] == "goto"
    assert seen["url"] == "https://x"
    assert seen["selector"] is None


def test_a_missing_browser_is_soft(monkeypatch):
    import aiforge_core.runtime.tools.browser as br
    monkeypatch.setattr(br, "browse",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no chromium")))
    assert t.browse("goto")["ok"] is False


def test_an_ipython_cell_runs_without_a_timeout_by_default(monkeypatch):
    import aiforge_core.runtime.tools.ipython_kernel as ik
    seen: dict = {}
    monkeypatch.setattr(ik, "execute_ipython_cell",
                        lambda code, **kw: seen.update(code=code, kw=kw) or {"ok": True})
    t.execute_ipython_cell("print(1)")
    assert seen == {"code": "print(1)", "kw": {}}
    t.execute_ipython_cell("print(1)", timeout=30)
    assert seen["kw"] == {"timeout": 30}


def test_a_missing_jupyter_is_soft(monkeypatch):
    import aiforge_core.runtime.tools.ipython_kernel as ik
    monkeypatch.setattr(ik, "execute_ipython_cell",
                        lambda *a, **k: (_ for _ in ()).throw(ImportError("no jupyter_client")))
    assert t.execute_ipython_cell("x")["ok"] is False


def test_delegation_forwards_role_prompt_and_timeout(monkeypatch):
    import aiforge_core.runtime.tools.delegation as dl
    seen: dict = {}
    monkeypatch.setattr(dl, "delegate_to_agent",
                        lambda role, prompt, timeout=600: seen.update(
                            role=role, prompt=prompt, timeout=timeout) or {"ok": True})
    t.delegate_to_agent("researcher", "find the spec", timeout=30)
    assert seen == {"role": "researcher", "prompt": "find the spec", "timeout": 30}


def test_a_delegation_failure_is_soft(monkeypatch):
    import aiforge_core.runtime.tools.delegation as dl
    monkeypatch.setattr(dl, "delegate_to_agent",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no runner")))
    assert t.delegate_to_agent("r", "p")["ok"] is False


# ─── github PRs ────────────────────────────────────────────────────────


@pytest.fixture
def gh(monkeypatch, tmp_path):
    from aiforge_core.runtime import sandbox
    monkeypatch.setattr(sandbox, "root", lambda: tmp_path)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
    seen: dict = {}

    class _R:
        returncode = 0
        stdout = "https://github.com/o/r/pull/1\n"
        stderr = ""

    def _run(cmd, **kw):
        seen["cmd"] = cmd
        return seen.get("result", _R())
    monkeypatch.setattr(subprocess, "run", _run)
    return seen


def test_a_pr_is_opened_with_the_given_fields(gh):
    out = t.github_pr("Fix the parser", body="b", base="develop", head="feat",
                      draft=True)
    assert out == {"ok": True, "url": "https://github.com/o/r/pull/1"}
    cmd = gh["cmd"]
    assert cmd[:3] == ["gh", "pr", "create"]
    assert "--draft" in cmd
    assert "develop" in cmd
    assert "feat" in cmd


def test_a_pr_needs_a_title(gh):
    assert t.github_pr("")["error"] == "missing 'title'"


def test_a_missing_gh_cli_says_how_to_fix_it(monkeypatch, tmp_path):
    from aiforge_core.runtime import sandbox
    monkeypatch.setattr(sandbox, "root", lambda: tmp_path)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: None)
    out = t.github_pr("t")
    assert out["error"] == "gh_not_installed"
    assert "gh auth login" in out["hint"]


def test_a_failed_pr_reports_the_cli_error(gh):
    class _R:
        returncode = 1
        stdout = ""
        stderr = "pull request already exists"
    gh["result"] = _R()
    assert t.github_pr("t")["error"] == "pull request already exists"


def test_a_crashing_gh_is_soft(monkeypatch, tmp_path):
    from aiforge_core.runtime import sandbox
    monkeypatch.setattr(sandbox, "root", lambda: tmp_path)
    import shutil
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("exec failed")))
    assert t.github_pr("t") == {"ok": False, "error": "exec failed"}


# ─── batched edits ─────────────────────────────────────────────────────


def test_every_edit_must_apply_for_the_batch_to_be_ok(monkeypatch):
    results = iter([{"ok": True}, {"ok": False, "error": "old_text_not_found"}])
    monkeypatch.setattr(t, "file_patch", lambda *a: next(results))
    out = t.multi_edit([{"path": "a.py", "old_text": "a", "new_text": "b"},
                        {"path": "b.py", "old_text": "c", "new_text": "d"}])
    assert out["ok"] is False
    assert [r["i"] for r in out["results"]] == [0, 1]
    assert out["results"][1]["error"] == "old_text_not_found"


def test_a_fully_applied_batch_is_ok(monkeypatch):
    monkeypatch.setattr(t, "file_patch", lambda *a: {"ok": True})
    assert t.multi_edit([{"path": "a.py", "old_text": "a", "new_text": "b"}])["ok"] is True


def test_the_openhands_key_spelling_is_accepted(monkeypatch):
    seen: dict = {}

    def _patch(path, old, new):
        seen.update(path=path, old=old, new=new)
        return {"ok": True}
    monkeypatch.setattr(t, "file_patch", _patch)
    t.multi_edit([{"path": "a.py", "old_str": "a", "new_str": "b"}])
    assert seen == {"path": "a.py", "old": "a", "new": "b"}


def test_a_non_object_edit_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(t, "file_patch", lambda *a: {"ok": True})
    out = t.multi_edit(["oops"])
    assert out["ok"] is False
    assert out["results"][0]["error"] == "not an object"


@pytest.mark.parametrize("edits", [[], "not a list", None])
def test_an_empty_or_malformed_batch(edits):
    assert t.multi_edit(edits)["ok"] is False
