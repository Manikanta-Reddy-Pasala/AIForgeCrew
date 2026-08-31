"""Chat tools that change how the system behaves afterwards.

Most of these persist something — a scheduled job, a repo folder, a default
Jira project — so they are strict about what they accept: a folder that is not
a directory is refused rather than stored, and an unschedulable cron never
becomes a job.

The job builder carries the important rule: the script is RUN ONCE before it
is scheduled. A wrong JQL or filter would otherwise be scheduled as-is and
fire forever doing nothing, and a rejected build must leave no orphan script
behind. Creating a job with a name that already exists REPLACES it, because
the alternative is duplicates that all fire.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.chat_agent._tools import _misc as M


# ─── the job builder ───────────────────────────────────────────────────


@pytest.fixture()
def jobs(monkeypatch):
    from aiforge_core.jobs import parse, scripts, store
    state: dict = {
        "trial": {"ok": True, "stdout": "3 issues"},
        "written": "/jobs/daily.sh", "deleted": [], "created": None,
        "existing": [], "removed": [], "schedulable": True}
    monkeypatch.setattr(parse, "schedulable", lambda c: state["schedulable"])
    monkeypatch.setattr(parse, "next_runs", lambda c, n=1: ["2026-09-02T09:00"])
    monkeypatch.setattr(parse, "human_schedule", lambda c: "every day at 09:00")
    monkeypatch.setattr(scripts, "write_script",
                        lambda name, body: state["written"])
    monkeypatch.setattr(scripts, "run_script", lambda p: state["trial"])
    monkeypatch.setattr(scripts, "delete_script",
                        lambda p: state["deleted"].append(p))
    monkeypatch.setattr(scripts, "is_within_jobs_dir",
                        lambda p: str(p).startswith("/jobs/"))
    monkeypatch.setattr(store, "list_jobs", lambda: state["existing"])
    monkeypatch.setattr(store, "delete", lambda jid: state["removed"].append(jid))
    monkeypatch.setattr(store, "create",
                        lambda **kw: state.update(created=kw)
                        or {"id": 11, "next_run_at": kw["next_run_at"]})
    return state


def _create(**over):
    args = {"name": "daily triage", "cron": "0 9 * * *", "script": "echo hi"}
    args.update(over)
    return M._t_create_job_script(args, "/repo")


def test_a_job_is_written_tested_and_scheduled(jobs):
    res = _create(description="triage the queue")
    assert res["ok"] is True and res["job_id"] == 11
    assert res["script_path"] == "/jobs/daily.sh" and res["tested"] is True
    assert res["trial_output"] == "3 issues"
    assert res["human_schedule"] == "every day at 09:00"
    assert jobs["created"]["kind"] == "script"
    assert jobs["created"]["ticket_body"] == "triage the queue"


def test_a_script_that_fails_its_trial_is_never_scheduled(jobs):
    """A wrong JQL would otherwise fire forever doing nothing."""
    jobs["trial"] = {"ok": False, "returncode": 2, "stdout": "", "stderr": "bad JQL"}
    res = _create()
    assert res["ok"] is False and "trial run FAILED" in res["error"]
    assert "bad JQL" in res["error"]
    assert jobs["created"] is None


def test_a_rejected_build_leaves_no_orphan_script(jobs):
    jobs["trial"] = {"ok": False, "returncode": 1}
    _create()
    assert jobs["deleted"] == ["/jobs/daily.sh"]


def test_the_trial_can_be_skipped_for_a_destructive_script(jobs):
    jobs["trial"] = {"ok": False, "returncode": 1}
    res = _create(skip_test=True)
    assert res["ok"] is True and res["tested"] is False
    assert res["trial_output"] is None


def test_a_job_with_the_same_name_is_replaced_not_duplicated(jobs):
    """Otherwise every rebuild adds another job and they all fire."""
    jobs["existing"] = [{"id": 4, "name": "Daily Triage",
                         "script_path": "/jobs/old.sh"},
                        {"id": 5, "name": "something else",
                         "script_path": "/jobs/other.sh"}]
    res = _create()
    assert res["replaced_jobs"] == [4] and jobs["removed"] == [4]
    assert "/jobs/old.sh" in jobs["deleted"]


def test_a_replaced_jobs_script_outside_the_jobs_dir_is_left_alone(jobs):
    jobs["existing"] = [{"id": 4, "name": "daily triage",
                         "script_path": "/etc/cron.d/thing"}]
    _create()
    assert jobs["deleted"] == [] and jobs["removed"] == [4]


def test_a_dedupe_failure_never_blocks_the_create(jobs, monkeypatch):
    from aiforge_core.jobs import store
    monkeypatch.setattr(store, "list_jobs",
                        lambda: (_ for _ in ()).throw(OSError("db")))
    assert _create()["ok"] is True


def test_an_unschedulable_cron_is_refused(jobs):
    jobs["schedulable"] = False
    res = _create(cron="whenever")
    assert res["ok"] is False and "unschedulable cron" in res["error"]


@pytest.mark.parametrize("missing", ["name", "cron", "script"])
def test_the_three_essentials_are_required(jobs, missing):
    res = _create(**{missing: "  "})
    assert res["error"] == "need name, cron, and script"


def test_a_store_failure_is_a_soft_error(jobs, monkeypatch):
    from aiforge_core.jobs import store
    monkeypatch.setattr(store, "create",
                        lambda **kw: (_ for _ in ()).throw(OSError("disk")))
    assert _create() == {"ok": False, "error": "disk"}


# ─── repo folders ──────────────────────────────────────────────────────


@pytest.fixture()
def repos(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    root = tmp_path / "codeRepo"
    (root / "widgets" / ".git").mkdir(parents=True)
    (root / "notes").mkdir()
    return root


def test_a_repo_folder_is_persisted(repos, tmp_path):
    res = M._t_set_repo_folder({"repo": "widgets",
                                "path": str(repos / "widgets")}, "/cwd")
    assert res["ok"] is True
    assert M._t_resolve_repo({"name": "widgets"}, "/cwd")["ok"] is True


def test_a_folder_that_is_not_there_is_refused(repos):
    res = M._t_set_repo_folder({"repo": "widgets", "path": "/nope"}, "/cwd")
    assert res["ok"] is False and "not a directory" in res["error"]


def test_both_the_repo_and_the_path_are_required(repos):
    assert M._t_set_repo_folder({"repo": "w"}, "/cwd")["error"] == \
        "need repo and path"


def test_the_global_base_folder_is_persisted(repos):
    assert M._t_set_repo_root({"path": str(repos)}, "/cwd")["ok"] is True
    assert M._t_list_repos({}, "/cwd")["default_root"] == str(repos)


def test_a_base_folder_that_is_not_there_is_refused(repos):
    assert M._t_set_repo_root({"path": "/nope"}, "/cwd")["ok"] is False
    assert M._t_set_repo_root({"path": " "}, "/cwd")["error"] == "need path"


def test_only_git_checkouts_count_as_repos_under_the_base(repos):
    M._t_set_repo_root({"path": str(repos)}, "/cwd")
    out = M._t_list_repos({}, "/cwd")
    assert out["repos_under_root"] == ["widgets"], "notes/ has no .git"


def test_an_unreadable_base_folder_still_lists_the_explicit_paths(repos,
                                                                  monkeypatch):
    M._t_set_repo_folder({"repo": "widgets", "path": str(repos / "widgets")},
                         "/cwd")
    M._t_set_repo_root({"path": str(repos)}, "/cwd")
    monkeypatch.setattr(M, "re", M.re)          # keep module intact
    import os
    monkeypatch.setattr(os, "listdir",
                        lambda p: (_ for _ in ()).throw(OSError("perm")))
    out = M._t_list_repos({}, "/cwd")
    assert out["repos_under_root"] == [] and "widgets" in out["paths"]


def test_a_loose_repo_name_resolves(repos):
    M._t_set_repo_root({"path": str(repos)}, "/cwd")
    assert M._t_resolve_repo({"repo": "Widgets"}, "/cwd")["ok"] is True


# ─── integration defaults ──────────────────────────────────────────────


@pytest.fixture()
def integrations(monkeypatch):
    from aiforge_core.config import integrations as cfg
    seen: dict = {}
    monkeypatch.setattr(cfg, "set_",
                        lambda tool, values: seen.update(tool=tool, **values))
    return seen


def test_a_default_jira_project_is_stored(integrations):
    res = M._t_set_integration_default({"tool": "jira", "value": "ENG"}, "/c")
    assert res["ok"] is True and res["default_project"] == "ENG"
    assert integrations == {"tool": "jira", "default_project": "ENG"}


def test_a_default_confluence_space_is_stored(integrations):
    res = M._t_set_integration_default({"tool": "Confluence", "value": "DEV"},
                                       "/c")
    assert res["default_space"] == "DEV"


def test_only_the_two_known_integrations_are_accepted(integrations):
    assert M._t_set_integration_default({"tool": "slack", "value": "x"},
                                        "/c")["ok"] is False


def test_a_default_needs_a_value(integrations):
    assert "missing 'value'" in M._t_set_integration_default(
        {"tool": "jira"}, "/c")["error"]


def test_an_unwritable_config_is_a_soft_error(monkeypatch):
    from aiforge_core.config import integrations as cfg
    monkeypatch.setattr(cfg, "set_",
                        lambda t, v: (_ for _ in ()).throw(OSError("ro")))
    assert M._t_set_integration_default({"tool": "jira", "value": "E"},
                                        "/c")["ok"] is False


# ─── the cross-entity dossier ──────────────────────────────────────────


@pytest.fixture()
def gather(monkeypatch):
    from aiforge_core.runtime import context_gather as cg
    seen: dict = {}
    monkeypatch.setattr(cg, "gather",
                        lambda kind, key, force=False, role=None:
                        seen.update(kind=kind, key=key, force=force, role=role)
                        or {"ok": True})
    return seen


def test_a_jira_key_is_recognised_without_being_told(gather):
    M._t_context_gather({"key": "eng-42"}, "/c")
    assert gather["kind"] == "jira" and gather["key"] == "ENG-42", \
        "and normalised to uppercase"


def test_a_numeric_id_is_a_confluence_page(gather):
    M._t_context_gather({"id": "123456"}, "/c")
    assert gather["kind"] == "confluence" and gather["key"] == "123456"


def test_an_explicit_kind_is_honoured(gather):
    M._t_context_gather({"kind": "confluence", "key": "ENG-42"}, "/c")
    assert gather["kind"] == "confluence"


def test_a_refresh_can_be_forced(gather):
    M._t_context_gather({"key": "ENG-1", "force": True}, "/c")
    assert gather["force"] is True and gather["role"] == "chat"


@pytest.mark.parametrize("args", [{}, {"kind": "jira"},
                                  {"kind": "slack", "key": "x"}])
def test_a_gather_with_nothing_to_look_up_is_refused(gather, args):
    assert M._t_context_gather(args, "/c")["ok"] is False


# ─── the managed notes ─────────────────────────────────────────────────


@pytest.fixture()
def notes(monkeypatch):
    from aiforge_core.runtime import note_curator, work_notes
    state: dict = {"primary": "/work/jira/ENG-1/ticket.md", "inside": True,
                   "seen": {}}
    monkeypatch.setattr(note_curator, "primary_note_for_cwd",
                        lambda cwd: state["primary"])
    monkeypatch.setattr(note_curator, "curate_note",
                        lambda path, cwd=None: state["seen"].update(path=path)
                        or {"ok": True, "changes": []})
    monkeypatch.setattr(note_curator, "_inside_work_root",
                        lambda p: state["inside"])
    monkeypatch.setattr(work_notes, "consolidate_note",
                        lambda path, text, role=None:
                        state["seen"].update(path=path, text=text, role=role)
                        or {"ok": True})
    return state


def test_curating_defaults_to_the_bound_contexts_note(notes):
    assert M._t_note_curate({}, "/work/jira/ENG-1")["ok"] is True
    assert notes["seen"]["path"] == "/work/jira/ENG-1/ticket.md"


def test_an_explicit_note_path_is_used(notes):
    M._t_note_curate({"path": "/work/jira/ENG-2/ticket.md"}, "/c")
    assert notes["seen"]["path"] == "/work/jira/ENG-2/ticket.md"


def test_outside_a_context_workspace_there_is_nothing_to_curate(notes):
    notes["primary"] = ""
    assert "no managed note found" in M._t_note_curate({}, "/repo")["error"]


def test_new_knowledge_is_folded_into_the_note(notes):
    res = M._t_note_consolidate({"text": "the deploy needs sudo"}, "/work")
    assert res["ok"] is True and notes["seen"]["role"] == "learner"
    assert notes["seen"]["text"] == "the deploy needs sudo"


def test_folding_needs_something_to_fold(notes):
    assert "pass 'text'" in M._t_note_consolidate({}, "/work")["error"]


def test_a_path_outside_the_managed_root_is_refused(notes):
    """Same boundary that lets this tool stay ungated."""
    notes["inside"] = False
    res = M._t_note_consolidate({"text": "x", "path": "/etc/passwd"}, "/work")
    assert res["ok"] is False and "outside the managed work root" in res["error"]


def test_folding_outside_a_context_workspace_says_so(notes):
    notes["primary"] = ""
    assert "no managed note found" in M._t_note_consolidate({"text": "x"},
                                                            "/repo")["error"]


# ─── thin pass-throughs ────────────────────────────────────────────────


def test_the_runtime_installer_is_reached(monkeypatch):
    from aiforge_core.runtime.tools import ensure_runtime as er
    monkeypatch.setattr(er, "ensure_runtime", lambda tools: {"ok": True,
                                                             "tools": tools})
    assert M._t_ensure_runtime({"tools": ["go"]}, "/c") == {"ok": True,
                                                            "tools": ["go"]}


def test_a_broken_installer_is_a_soft_error(monkeypatch):
    from aiforge_core.runtime.tools import ensure_runtime as er
    monkeypatch.setattr(er, "ensure_runtime",
                        lambda tools: (_ for _ in ()).throw(OSError("apt")))
    assert M._t_ensure_runtime({}, "/c")["ok"] is False


def test_the_project_runner_defaults_to_detect(monkeypatch):
    from aiforge_core.runtime.tools import project_runner as pr
    seen: dict = {}
    monkeypatch.setattr(pr, "project",
                        lambda action, cwd, timeout: seen.update(
                            action=action, cwd=cwd, timeout=timeout)
                        or {"ok": True})
    M._t_project({}, "/repo")
    assert seen == {"action": "detect", "cwd": "/repo", "timeout": 1800}


def test_the_project_runner_takes_its_own_cwd_and_timeout(monkeypatch):
    from aiforge_core.runtime.tools import project_runner as pr
    seen: dict = {}
    monkeypatch.setattr(pr, "project",
                        lambda action, cwd, timeout: seen.update(
                            action=action, cwd=cwd, timeout=timeout)
                        or {"ok": True})
    M._t_project({"action": "test", "cwd": "/other", "timeout": "60"}, "/repo")
    assert seen == {"action": "test", "cwd": "/other", "timeout": 60}


def test_a_project_run_that_blows_up_is_a_soft_error(monkeypatch):
    from aiforge_core.runtime.tools import project_runner as pr
    monkeypatch.setattr(pr, "project",
                        lambda **kw: (_ for _ in ()).throw(OSError("no maven")))
    assert M._t_project({}, "/repo")["ok"] is False


@pytest.mark.parametrize("tool,module,fn", [
    (M._t_email_send, "email_tool", "email_send"),
    (M._t_email_read, "email_tool", "email_read"),
    (M._t_serve, "serve", "serve"),
    (M._t_stop_service, "serve", "stop_service"),
    (M._t_list_services, "serve", "list_services"),
])
def test_each_pass_through_reaches_its_implementation(tool, module, fn,
                                                      monkeypatch):
    import importlib
    mod = importlib.import_module(f"aiforge_core.runtime.tools.{module}")
    monkeypatch.setattr(mod, fn,
                        lambda args, cwd: {"ok": True, "fn": fn, "cwd": cwd})
    assert tool({"a": 1}, "/repo") == {"ok": True, "fn": fn, "cwd": "/repo"}
