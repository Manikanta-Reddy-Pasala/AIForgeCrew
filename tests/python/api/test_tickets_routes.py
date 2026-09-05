"""The ticket API: create, patch, route, attachments, intervention, streaming.

The property with the most history behind it is attachment atomicity. The row
is written before the files, so a failed upload used to 500 with the ticket
already saved: the runner picked up a half-made ticket and every operator
retry added another orphan. Attachments are part of the save, so a store
failure now rolls the row back and names the directory that was unwritable.

The rest is shape: a duration that keeps counting while the run is live, a
route override that records whether a human or the detector chose, and an
event stream that closes on a terminal status or when the run pauses for an
answer.
"""
from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from aiforge_core.api.routes import tickets as tk

UTC = timezone.utc


class _T:
    """A ticket row object, as the store hands one back."""

    def __init__(self, **kw):
        self.id = kw.get("id", 1)
        self.identifier = kw.get("identifier", "ONE-1")
        self.title = kw.get("title", "Fix the parser")
        self.body = kw.get("body", "")
        self.status = kw.get("status", "todo")
        self.priority = kw.get("priority", "medium")
        self.assignee_role = kw.get("assignee_role")
        self.parent_id = kw.get("parent_id")
        self.branch = kw.get("branch")
        self.project = kw.get("project")
        self.labels = kw.get("labels", [])
        self.metadata = kw.get("metadata", {})
        self.created_at = kw.get("created_at")
        self.updated_at = kw.get("updated_at")
        self.completed_at = kw.get("completed_at")
        self.route = kw.get("route", "code")
        self.route_workflow = kw.get("route_workflow")
        self.route_source = kw.get("route_source", "auto")
        self.route_confidence = kw.get("route_confidence")


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(tk.router)
    return TestClient(app)


@pytest.fixture
def store(monkeypatch, tmp_path):
    """A stubbed ticket store + a temp attachment base."""
    state: dict = {"ticket": _T(), "created": None, "deleted": [], "events": [],
                   "patched": [], "status": []}
    monkeypatch.setattr(tk, "_ticket_files_base", lambda: tmp_path / "ticket-files")

    def _create(**kw):
        state["created"] = kw
        return _T(**{k: v for k, v in kw.items()
                     if k in ("title", "body", "priority", "project", "labels",
                              "metadata", "route", "route_workflow",
                              "route_source", "route_confidence", "parent_id",
                              "assignee_role")})
    monkeypatch.setattr(tk.tickets_mod, "create", _create)
    monkeypatch.setattr(tk.tickets_mod, "get",
                        lambda ident: state["ticket"] if ident == "ONE-1" else None)
    monkeypatch.setattr(tk.tickets_mod, "delete",
                        lambda ident: bool(state["deleted"].append(ident)) or ident == "ONE-1")
    monkeypatch.setattr(tk.tickets_mod, "set_branch",
                        lambda tid, branch: state.update(branch=branch))
    monkeypatch.setattr(tk.tickets_mod, "update_status",
                        lambda tid, st, role=None, metadata_patch=None:
                        state["status"].append((st, role, metadata_patch)))
    monkeypatch.setattr(tk.tickets_mod, "patch_fields",
                        lambda tid, fields=None, metadata_patch=None:
                        state["patched"].append((fields, metadata_patch)))
    monkeypatch.setattr(tk.tickets_mod, "comments", lambda tid, n=100: state["events"])
    monkeypatch.setattr(tk.tickets_mod, "list_tickets",
                        lambda **kw: state.get("rows", []))
    monkeypatch.setattr(tk.tickets_mod, "add_comment", lambda tid, author, body: 99)
    monkeypatch.setattr(tk.tickets_mod, "reset_all", lambda: 4)
    return state


# ─── row shaping ───────────────────────────────────────────────────────


def _row(**kw):
    base = {"id": 1, "identifier": "ONE-1", "title": "t", "body": "b",
            "status": "todo", "priority": "medium", "assignee_role": None,
            "parent_id": None, "branch": None, "project": None, "labels": None,
            "metadata": None, "updated_at": None}
    base.update(kw)
    return base


def test_a_run_that_has_not_started_has_no_duration():
    assert tk._duration_s(None, None, "todo") is None


def test_a_finished_run_measures_start_to_completion():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    assert tk._duration_s(start, start + timedelta(seconds=90), "done") == 90.0


def test_a_live_run_keeps_counting():
    """A completed_at on a non-terminal ticket is stale — measure to NOW."""
    start = datetime.now(UTC) - timedelta(seconds=5)
    stale = start + timedelta(seconds=1)
    assert tk._duration_s(start, stale, "in_progress") >= 4.0


def test_a_naive_timestamp_is_treated_as_utc():
    start = datetime(2026, 1, 1)                       # no tzinfo
    end = datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)
    assert tk._duration_s(start, end, "done") == 30.0


def test_a_row_defaults_its_route_and_collections():
    out = tk._ticket_row_out(_row())
    assert out["route"] == "code"
    assert out["route_source"] == "auto"
    assert out["labels"] == []
    assert out["metadata"] == {}
    assert out["created_at"] is None


def test_an_assignee_role_is_canonicalised(monkeypatch):
    monkeypatch.setattr(tk._cfg, "canonical_role", lambda r: f"canon:{r}")
    assert tk._ticket_row_out(_row(assignee_role="dev"))["assignee_role"] == "canon:dev"


def test_an_event_row_is_shaped():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    out = tk._event_row_out({"id": 1, "ticket_id": 2, "agent_role": "doer",
                             "kind": "stage_done", "body": None,
                             "metadata": None, "created_at": now})
    assert out["body"] == ""
    assert out["metadata"] == {}
    assert out["created_at"] == now.isoformat()


# ─── branch derivation ─────────────────────────────────────────────────


@pytest.mark.parametrize("title,branch", [
    ("Fix the parser", "aiforge/ONE-1-fix-the-parser"),
    ("", "aiforge/ONE-1"),
    ("!!!", "aiforge/ONE-1"),
    ("A" * 100, "aiforge/ONE-1-" + "a" * 40),
])
def test_a_branch_is_derived_from_the_title(title, branch):
    assert tk._derive_branch("ONE-1", title) == branch


# ─── attachments ───────────────────────────────────────────────────────


def _file(name, data=b"hello"):
    return tk.AttachedFile(name=name,
                           content_b64=base64.b64encode(data).decode())


def test_attachments_are_written_and_described(store, tmp_path):
    out = tk._persist_ticket_attachments("ONE-1", [_file("spec.txt")])
    assert out == [{"name": "spec.txt", "size": 5,
                    "path": ".aiforge/ticket-files/ONE-1/spec.txt",
                    "abs_path": str(tmp_path / "ticket-files/ONE-1/spec.txt")}]
    assert (tmp_path / "ticket-files/ONE-1/spec.txt").read_bytes() == b"hello"


def test_an_attachment_name_cannot_escape_its_ticket_dir(store, tmp_path):
    out = tk._persist_ticket_attachments("ONE-1", [_file("../../etc/passwd")])
    assert out[0]["name"] == "passwd"
    assert (tmp_path / "ticket-files/ONE-1/passwd").exists()


def test_an_undecodable_attachment_is_skipped(store):
    bad = tk.AttachedFile(name="x.bin", content_b64="!!!not base64!!!")
    assert tk._persist_ticket_attachments("ONE-1", [bad]) == []


def test_an_unwritable_store_names_the_directory(store, monkeypatch, tmp_path):
    """A bare OSError surfaced as an opaque 500; the real cause is almost
    always a missing or root-owned directory."""
    import pathlib
    monkeypatch.setattr(pathlib.Path, "mkdir",
                        lambda self, **kw: (_ for _ in ()).throw(PermissionError(13, "denied")))
    attachment = _file("a.txt")
    with pytest.raises(HTTPException) as ei:
        tk._persist_ticket_attachments("ONE-1", [attachment])
    assert ei.value.status_code == 500
    assert "could not store attachments under" in str(ei.value.detail)
    assert "writable" in str(ei.value.detail)


def test_a_failed_write_is_not_reported_as_saved(store, monkeypatch):
    import pathlib
    monkeypatch.setattr(pathlib.Path, "write_bytes",
                        lambda self, data: (_ for _ in ()).throw(OSError(28, "no space")))
    attachment = _file("a.txt")
    with pytest.raises(HTTPException, match="could not store attachments under"):
        tk._persist_ticket_attachments("ONE-1", [attachment])


def test_named_attachments_are_removed(store, tmp_path):
    tk._persist_ticket_attachments("ONE-1", [_file("a.txt"), _file("b.txt")])
    assert tk._remove_ticket_attachments("ONE-1", ["a.txt", "gone.txt"]) == ["a.txt"]
    assert not (tmp_path / "ticket-files/ONE-1/a.txt").exists()
    assert (tmp_path / "ticket-files/ONE-1/b.txt").exists()


def test_removal_names_are_reduced_to_basenames(store, tmp_path):
    tk._persist_ticket_attachments("ONE-1", [_file("a.txt")])
    assert tk._remove_ticket_attachments("ONE-1", ["../../a.txt", ""]) == ["a.txt"]


# ─── creating ──────────────────────────────────────────────────────────


@pytest.fixture
def create_env(monkeypatch, store):
    monkeypatch.setattr(tk._cfg, "canonical_role", lambda r: r)
    import aiforge_core.workflows as wf
    monkeypatch.setattr(wf, "detect_route",
                        lambda **kw: type("D", (), {"kind": "code", "workflow_id": None,
                                                    "rationale": "looks like code",
                                                    "confidence": 0.8})())
    return store


def test_a_ticket_is_created_with_a_derived_branch(client, create_env):
    body = client.post("/api/tickets", json={"title": "Fix the parser"}).json()
    assert body["identifier"] == "ONE-1"
    assert create_env["branch"] == "aiforge/ONE-1-fix-the-parser"


def test_the_detector_decides_the_route_by_default(client, create_env):
    client.post("/api/tickets", json={"title": "t", "body": "b"})
    md = create_env["created"]["metadata"]
    assert create_env["created"]["route_source"] == "auto"
    assert md["route_rationale"] == "looks like code"


def test_a_manual_route_is_recorded_as_manual(client, create_env):
    client.post("/api/tickets", json={"title": "t", "route": "workflow",
                                      "route_workflow": "wf-1"})
    assert create_env["created"]["route_source"] == "manual"
    assert create_env["created"]["route_confidence"] == 1.0


def test_a_workflow_route_needs_a_workflow_id(client, create_env):
    r = client.post("/api/tickets", json={"title": "t", "route": "workflow"})
    assert r.status_code == 400
    assert "requires route_workflow" in r.json()["detail"]


def test_a_broken_detector_never_breaks_a_ticket_post(client, create_env, monkeypatch):
    import aiforge_core.workflows as wf
    monkeypatch.setattr(wf, "detect_route",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("no model")))
    client.post("/api/tickets", json={"title": "t"})
    md = create_env["created"]["metadata"]
    assert md["route_error"] == "no model"
    assert create_env["created"]["route"] == "code"


@pytest.mark.parametrize("raw,normalised", [
    ("qa", "qa"), ("PROD", "prod"), ("none", "none"), ("typo", "none"),
    (None, "none"),
])
def test_the_deploy_target_cannot_be_armed_by_a_typo(raw, normalised):
    assert tk._deploy_target(raw) == normalised


def test_max_turns_lands_in_metadata(client, create_env):
    client.post("/api/tickets", json={"title": "t", "max_turns": 12})
    assert create_env["created"]["metadata"]["max_turns"] == 12


def test_an_unknown_parent_is_a_400(client, create_env):
    r = client.post("/api/tickets", json={"title": "t",
                                          "parent_identifier": "ONE-999"})
    assert r.status_code == 400
    assert "not found" in r.json()["detail"]


def test_a_known_parent_is_linked(client, create_env):
    client.post("/api/tickets", json={"title": "t", "parent_identifier": "ONE-1"})
    assert create_env["created"]["parent_id"] == 1


def test_a_failed_attachment_save_rolls_the_ticket_back(client, create_env, monkeypatch):
    """Otherwise the runner picks up a half-made ticket and every retry adds
    another orphan."""
    monkeypatch.setattr(tk, "_persist_ticket_attachments",
                        lambda ident, files: (_ for _ in ()).throw(
                            tk.HTTPException(500, "store unwritable")))
    r = client.post("/api/tickets",
                    json={"title": "t",
                          "attached_files": [{"name": "a.txt", "content_b64": "eA=="}]})
    assert r.status_code == 500
    assert create_env["deleted"] == ["ONE-1"]


def test_a_failed_rollback_is_logged_not_raised(client, create_env, monkeypatch):
    monkeypatch.setattr(tk, "_persist_ticket_attachments",
                        lambda ident, files: (_ for _ in ()).throw(
                            tk.HTTPException(500, "store unwritable")))
    monkeypatch.setattr(tk.tickets_mod, "delete",
                        lambda ident: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert client.post("/api/tickets",
                       json={"title": "t",
                             "attached_files": [{"name": "a.txt",
                                                 "content_b64": "eA=="}]}).status_code == 500


def test_attached_files_are_stamped_into_metadata(client, create_env):
    client.post("/api/tickets",
                json={"title": "t",
                      "attached_files": [{"name": "a.txt", "content_b64": "eA=="}]})
    patch = [s for s in create_env["status"] if s[2]][-1][2]
    assert patch["attached_files"][0]["name"] == "a.txt"


def test_a_failed_metadata_stamp_does_not_fail_the_create(client, create_env, monkeypatch):
    monkeypatch.setattr(tk.tickets_mod, "update_status",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db gone")))
    assert client.post("/api/tickets",
                       json={"title": "t",
                             "attached_files": [{"name": "a.txt",
                                                 "content_b64": "eA=="}]}).status_code == 200


def test_an_existing_branch_is_not_overwritten(store, monkeypatch):
    t = _T(branch="feature/manual")
    monkeypatch.setattr(tk.tickets_mod, "set_branch",
                        lambda *a: pytest.fail("overwrote an existing branch"))
    tk._ensure_branch(t)
    assert t.branch == "feature/manual"


def test_a_branch_write_failure_keeps_the_derived_name(store, monkeypatch):
    t = _T()
    monkeypatch.setattr(tk.tickets_mod, "set_branch",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("db gone")))
    tk._ensure_branch(t)
    assert t.branch.startswith("aiforge/ONE-1")


# ─── reading ───────────────────────────────────────────────────────────


def test_tickets_are_listed_with_a_status_filter(client, store, monkeypatch):
    seen: dict = {}

    def _list(role=None, statuses=None, parent_identifier=None, limit=None):
        seen.update(role=role, statuses=statuses, parent=parent_identifier,
                    limit=limit)
        return [_row()]
    monkeypatch.setattr(tk.tickets_mod, "list_tickets", _list)
    client.get("/api/tickets?status=todo,in_progress&role=doer&limit=5")
    assert seen == {"role": "doer", "statuses": ["todo", "in_progress"],
                    "parent": None, "limit": 5}


def test_the_list_limit_is_capped(client, store):
    assert client.get("/api/tickets?limit=9999").status_code == 422


def test_a_ticket_detail_carries_events_children_and_timings(client, store, monkeypatch):
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store["events"] = [
        {"id": 1, "ticket_id": 1, "agent_role": "doer", "kind": "stage_done",
         "body": "", "metadata": {"stage": "doer", "duration_s": 12, "extra": 1},
         "created_at": now},
        {"id": 2, "ticket_id": 1, "agent_role": "doer", "kind": "comment",
         "body": "hi", "metadata": {}, "created_at": now},
    ]
    store["rows"] = [_row(identifier="ONE-2", parent_id=1)]
    monkeypatch.setattr(tk.tickets_mod, "get_enriched", lambda ident: _row())
    from aiforge_core.tickets import subtasks
    monkeypatch.setattr(subtasks, "get_subtasks", lambda tid: [{"slug": "a"}])
    monkeypatch.setattr(subtasks, "progress",
                        lambda subs: {"total": 1, "done": 0, "counts": {},
                                      "fraction": 0.0})
    body = client.get("/api/tickets/ONE-1").json()
    assert len(body["events"]) == 2
    assert [c["identifier"] for c in body["children"]] == ["ONE-2"]
    assert body["timings"] == [{"stage": "doer", "duration_s": 12,
                                "at": now.isoformat(), "extra": {"extra": 1}}]
    assert body["subtask_progress"]["total"] == 1


def test_a_broken_subtask_store_still_renders_the_ticket(client, store, monkeypatch):
    monkeypatch.setattr(tk.tickets_mod, "get_enriched", lambda ident: _row())
    from aiforge_core.tickets import subtasks
    monkeypatch.setattr(subtasks, "get_subtasks",
                        lambda tid: (_ for _ in ()).throw(RuntimeError("db gone")))
    body = client.get("/api/tickets/ONE-1").json()
    assert body["subtasks"] == []
    assert body["subtask_progress"]["total"] == 0


def test_a_missing_ticket_is_a_404(client, store, monkeypatch):
    monkeypatch.setattr(tk.tickets_mod, "get_enriched", lambda ident: None)
    assert client.get("/api/tickets/ONE-9").status_code == 404


# ─── patching ──────────────────────────────────────────────────────────


@pytest.fixture
def patch_env(client, store, monkeypatch):
    monkeypatch.setattr(tk._cfg, "canonical_role", lambda r: r)
    monkeypatch.setattr(tk.tickets_mod, "VALID_STATUS", {"todo", "done"})
    monkeypatch.setattr(tk.tickets_mod, "get_enriched", lambda ident: _row())
    from aiforge_core.tickets import subtasks
    monkeypatch.setattr(subtasks, "get_subtasks", lambda tid: [])
    monkeypatch.setattr(subtasks, "progress",
                        lambda subs: {"total": 0, "done": 0, "counts": {},
                                      "fraction": 0.0})
    return store


def test_a_status_change_is_recorded_as_human(client, patch_env):
    client.patch("/api/tickets/ONE-1", json={"status": "done"})
    assert ("done", "human", None) in patch_env["status"]


def test_an_invalid_status_is_a_400(client, patch_env):
    r = client.patch("/api/tickets/ONE-1", json={"status": "nope"})
    assert r.status_code == 400
    assert "bad status" in r.json()["detail"]


def test_patching_a_missing_ticket_is_a_404(client, patch_env):
    assert client.patch("/api/tickets/ONE-9", json={"body": "x"}).status_code == 404


def test_editable_fields_are_forwarded(client, patch_env):
    client.patch("/api/tickets/ONE-1",
                 json={"body": "new body", "labels": ["a"], "assignee_role": "doer"})
    fields, _md = patch_env["patched"][-1]
    assert fields == {"assignee_role": "doer", "labels": ["a"], "body": "new body"}


def test_editing_attachments_replaces_the_whole_list(patch_env, monkeypatch):
    """jsonb '||' shallow-merges, so the FULL list is what covers add+remove."""
    t = _T(metadata={"attached_files": [{"name": "old.txt"}, {"name": "keep.txt"}]})
    monkeypatch.setattr(tk, "_remove_ticket_attachments",
                        lambda ident, names: ["old.txt"])
    monkeypatch.setattr(tk, "_persist_ticket_attachments",
                        lambda ident, files: [{"name": "new.txt"}])

    class _P:
        remove_files = ["old.txt"]
        attached_files = [object()]
    assert tk._edited_attachments(t, _P()) == [{"name": "keep.txt"},
                                               {"name": "new.txt"}]


def test_max_turns_can_be_patched(patch_env):
    class _P:
        metadata = {"k": "v"}
        max_turns = 5
        remove_files: list = []
        attached_files: list = []
    assert tk._patch_metadata(_T(), _P()) == {"k": "v", "max_turns": 5}


# ─── routing ───────────────────────────────────────────────────────────


def test_the_route_detector_can_be_previewed(client, monkeypatch):
    import aiforge_core.workflows.detector as det
    seen: dict = {}

    def _preview(body, title="", attachments=(), intent=None):
        seen.update(body=body, title=title)
        return {"kind": "workflow", "workflow_id": "wf-1"}
    monkeypatch.setattr(det, "preview", _preview)
    body = client.post("/api/workflows/preview",
                       json={"body": "deploy it", "title": "t"}).json()
    assert body["workflow_id"] == "wf-1"
    assert seen["body"] == "deploy it"


@pytest.fixture
def route_env(monkeypatch, store):
    import aiforge_core.workflows as wf
    monkeypatch.setattr(wf, "get", lambda wid: {"id": wid} if wid == "wf-1" else None)
    monkeypatch.setattr(tk.tickets_mod, "update_route",
                        lambda ident, **kw: _T(**kw) if ident == "ONE-1" else None)
    return store


def test_an_override_records_the_manual_source(client, route_env):
    body = client.put("/api/tickets/ONE-1/route",
                      json={"route": "workflow", "route_workflow": "wf-1"}).json()
    assert body["route"] == "workflow"
    assert body["route_source"] == "manual"


def test_a_workflow_override_needs_an_id(client, route_env):
    r = client.put("/api/tickets/ONE-1/route", json={"route": "workflow"})
    assert r.status_code == 400
    assert "requires route_workflow" in r.json()["detail"]


def test_an_unknown_workflow_id_is_a_400(client, route_env):
    r = client.put("/api/tickets/ONE-1/route",
                   json={"route": "workflow", "route_workflow": "nope"})
    assert r.status_code == 400
    assert "unknown workflow id" in r.json()["detail"]


def test_overriding_a_missing_ticket_is_a_404(client, route_env):
    r = client.put("/api/tickets/ONE-9/route", json={"route": "code"})
    assert r.status_code == 404


# ─── run-parallel, comments, delete, reset ─────────────────────────────


def test_run_parallel_starts_in_the_background(client, store, monkeypatch):
    started: list = []
    monkeypatch.setattr(tk, "_spawn", lambda fn, name=None: started.append(name) or fn())
    import aiforge_core.runtime.parallel_subtasks as ps
    ran: list = []
    monkeypatch.setattr(ps, "run_subtasks_parallel", lambda t: ran.append(t.identifier))
    r = client.post("/api/tickets/ONE-1/run-parallel")
    assert r.status_code == 202
    assert r.json() == {"started": True,
                                                 "identifier": "ONE-1"}
    assert started == ["parallel-ONE-1"]
    assert ran == ["ONE-1"]


def test_a_crash_in_the_background_run_is_logged(client, store, monkeypatch):
    monkeypatch.setattr(tk, "_spawn", lambda fn, name=None: fn())
    import aiforge_core.runtime.parallel_subtasks as ps
    monkeypatch.setattr(ps, "run_subtasks_parallel",
                        lambda t: (_ for _ in ()).throw(RuntimeError("no worktree")))
    assert client.post("/api/tickets/ONE-1/run-parallel").status_code == 202


def test_run_parallel_on_a_missing_ticket_is_a_404(client, store):
    assert client.post("/api/tickets/ONE-9/run-parallel").status_code == 404


def test_a_comment_returns_its_event_id(client, store):
    r = client.post("/api/tickets/ONE-1/comments", json={"body": "hi"})
    assert r.status_code == 201
    assert r.json() == {"event_id": 99}


def test_commenting_on_a_missing_ticket_is_a_404(client, store):
    assert client.post("/api/tickets/ONE-9/comments",
                       json={"body": "hi"}).status_code == 404


def test_deleting_a_ticket(client, store):
    assert client.delete("/api/tickets/ONE-1").status_code == 204
    assert client.delete("/api/tickets/ONE-9").status_code == 404


def test_reset_reports_how_many_were_deleted(client, store):
    assert client.post("/api/tickets/reset").json() == {"ok": True, "deleted": 4}


# ─── live intervention ─────────────────────────────────────────────────


@pytest.fixture
def task_dirs(monkeypatch, tmp_path):
    ga = tmp_path / "ga"
    (ga / "temp" / "aiforge-ONE-1-abc").mkdir(parents=True)
    (ga / "temp" / "aiforge-planner-ONE-1-def").mkdir(parents=True)
    monkeypatch.setenv("AIFORGE_GA_DIR", str(ga))
    return ga / "temp"


def test_both_the_agent_and_planner_dirs_are_found(task_dirs):
    dirs = tk._resolve_active_task_dirs("ONE-1")
    assert len(dirs) == 2
    assert any("planner" in d for d in dirs)


def test_no_ga_checkout_means_no_targets(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_GA_DIR", str(tmp_path / "gone"))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "nope"))
    assert tk._resolve_active_task_dirs("ONE-1") == []


def test_another_tickets_directory_is_not_a_target(task_dirs):
    (task_dirs / "aiforge-ONE-2-xyz").mkdir()
    dirs = tk._resolve_active_task_dirs("ONE-1")
    assert len(dirs) == 2
    assert not any("ONE-2" in d for d in dirs)


def test_a_file_named_like_a_task_dir_is_not_a_target(task_dirs):
    (task_dirs / "aiforge-ONE-1-note").write_text("not a task dir")
    assert len(tk._resolve_active_task_dirs("ONE-1")) == 2


def test_an_identifier_that_traverses_is_refused(task_dirs):
    """Refused outright — the id never reaches the filesystem call.

    The directory listing is filtered by NAME, so a traversing id could not
    escape the temp dir even if it got this far; it is rejected anyway,
    because a ticket id containing "../" is not a ticket id with a typo.
    """
    assert tk._resolve_active_task_dirs("../ONE-1") == []
    assert tk._resolve_active_task_dirs("") == []


def test_an_unreadable_temp_dir_yields_no_targets(task_dirs, monkeypatch):
    real, denied = os.scandir, os.path.realpath(str(task_dirs))

    def _denied(path):
        if str(path) == denied:      # the temp dir itself, not its ancestors
            raise PermissionError("nope")
        return real(path)
    monkeypatch.setattr(os, "scandir", _denied)
    assert tk._resolve_active_task_dirs("ONE-1") == []


@pytest.mark.parametrize("kind,expected", [("stop", ""), ("keyinfo", "note"),
                                           ("intervene", "note")])
def test_a_control_file_is_written_to_every_target(client, task_dirs, kind, expected):
    body = client.post(f"/api/tickets/ONE-1/intervene",
                       json={"kind": kind, "body": "note"}).json()
    assert len(body["written"]) == 2
    for d in body["written"]:
        assert open(os.path.join(d, f"_{kind}")).read() == expected


def test_an_unknown_intervention_kind_is_a_400(client, task_dirs):
    r = client.post("/api/tickets/ONE-1/intervene", json={"kind": "explode"})
    assert r.status_code == 400
    assert "must be one of" in r.json()["detail"]


def test_intervening_with_no_running_agent_is_a_404(client, monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_GA_DIR", str(tmp_path / "gone"))
    monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path / "nope"))
    assert client.post("/api/tickets/ONE-1/intervene",
                       json={"kind": "stop"}).status_code == 404


def test_an_unwritable_target_is_skipped(client, task_dirs, monkeypatch):
    real_open = open

    def _fussy(path, *a, **kw):
        if "planner" in str(path):
            raise PermissionError("read-only")
        return real_open(path, *a, **kw)
    monkeypatch.setattr("builtins.open", _fussy)
    body = client.post("/api/tickets/ONE-1/intervene",
                       json={"kind": "stop"}).json()
    assert len(body["written"]) == 1


# ─── clarification answers ─────────────────────────────────────────────


def test_an_answer_is_folded_in_and_the_ticket_requeued(client, store, monkeypatch):
    appended: list = []
    monkeypatch.setattr(tk.tickets_mod, "append_body",
                        lambda tid, text: appended.append(text))
    monkeypatch.setattr(tk.tickets_mod, "add_event", lambda *a: None)
    body = client.post("/api/tickets/ONE-1/answer",
                       json={"content": "  use an LRU  "}).json()
    assert body == {"ticket": "ONE-1", "status": "todo",
                    "trace_url": "/api/tickets/ONE-1/events/stream"}
    assert "## Clarification\nuse an LRU" in appended[0]
    assert store["status"][-1][2] == {"clarified": True, "awaiting_input": False}


def test_answering_a_missing_ticket_is_a_404(client, store):
    assert client.post("/api/tickets/ONE-9/answer",
                       json={"content": "x"}).status_code == 404


def test_an_empty_answer_is_rejected(client, store):
    assert client.post("/api/tickets/ONE-1/answer",
                       json={"content": ""}).status_code == 422


# ─── the event stream ──────────────────────────────────────────────────


def _events(gen, limit=10):
    out = []
    for chunk in gen:
        out.append(json.loads(chunk[len("data: "):]))
        if len(out) >= limit:
            break
    return out


def test_a_missing_ticket_streams_an_error(store, monkeypatch):
    monkeypatch.setattr(tk.tickets_mod, "get", lambda ident: None)
    assert _events(tk._ticket_event_stream("ONE-9")) == [
        {"kind": "error", "body": "ticket not found"}]


def test_the_stream_closes_on_a_terminal_status(store):
    store["ticket"] = _T(status="done")
    store["events"] = [{"id": 1, "kind": "comment", "agent_role": "doer",
                        "body": "hi", "metadata": {}, "created_at": None}]
    out = _events(tk._ticket_event_stream("ONE-1"))
    assert out[0]["kind"] == "comment"
    assert out[-1] == {"kind": "done", "status": "done"}


def test_a_blocked_run_closes_the_stream(store):
    store["ticket"] = _T(status="blocked")
    assert _events(tk._ticket_event_stream("ONE-1"))[-1] == {"kind": "done",
                                                             "status": "blocked"}


def test_the_stream_pauses_when_the_run_awaits_an_answer(store):
    store["ticket"] = _T(status="in_progress",
                         metadata={"awaiting_input": True,
                                   "clarify_questions": ["which db?"]})
    out = _events(tk._ticket_event_stream("ONE-1"))
    assert out[-1] == {"kind": "status", "status": "in_progress",
                       "awaiting_input": True, "clarify_questions": ["which db?"]}


def test_an_event_is_only_streamed_once(store):
    store["ticket"] = _T(status="done")
    store["events"] = [{"id": 1, "kind": "comment", "agent_role": "d",
                        "body": "hi", "metadata": {}, "created_at": None}]
    seen: set = set()
    assert len(list(tk._new_events(1, seen))) == 1
    assert list(tk._new_events(1, seen)) == []


def test_a_ticket_deleted_mid_stream_ends_it(store, monkeypatch):
    calls = {"n": 0}

    def _get(ident):
        calls["n"] += 1
        return _T(status="in_progress") if calls["n"] == 1 else None
    monkeypatch.setattr(tk.tickets_mod, "get", _get)
    assert _events(tk._ticket_event_stream("ONE-1")) == []
