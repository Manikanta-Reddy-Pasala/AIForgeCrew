"""Jobs API — preview saves nothing; save re-validates; run-now shares
the fire path and works while paused."""
from __future__ import annotations

import importlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_client(monkeypatch, tmp_path):
    monkeypatch.delenv("AIFORGE_PG_URL", raising=False)
    monkeypatch.delenv("AIFORGE_FORCE_PG", raising=False)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "mem"))
    monkeypatch.setenv("AIFORGE_MEMORY_BACKEND", "sqlite")
    monkeypatch.setenv("AIFORGE_MEMORY_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("AIFORGE_JOBS_DISABLE", "1")   # no live loop in tests
    import aiforge_core.config.env as envmod
    importlib.reload(envmod)
    import aiforge_core.tickets.backend_factory as bf
    importlib.reload(bf)
    bf.reset_backend_for_tests()
    import aiforge_core.tickets.store as store
    importlib.reload(store)
    import aiforge_core.api.api as api
    importlib.reload(api)
    return TestClient(api.app), api


_DRAFT = {"name": "digest", "cron": "0 8 * * *",
          "ticket_title": "Pull GitLab comments",
          "ticket_body": "Fetch and summarize.", "project": None}


def test_preview_saves_nothing(app_client, monkeypatch):
    client, _ = app_client
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: json.dumps(_DRAFT))
    r = client.post("/api/jobs/preview",
                    json={"instructions": "gitlab comments daily 8am"})
    assert r.status_code == 200
    out = r.json()
    assert out["ok"] is True
    assert out["draft"]["cron"] == "0 8 * * *"
    assert out["human_schedule"] == "Every day at 08:00"
    assert client.get("/api/jobs").json() == []          # nothing saved


def test_preview_parse_error_is_friendly_not_500(app_client, monkeypatch):
    client, _ = app_client
    monkeypatch.setattr("aiforge_core.llm.client.complete",
                        lambda *a, **k: "no json here")
    r = client.post("/api/jobs/preview", json={"instructions": "x y z"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_create_list_patch_delete_roundtrip(app_client):
    client, _ = app_client
    r = client.post("/api/jobs", json=_DRAFT)
    assert r.status_code == 201
    jid = r.json()["id"]
    assert r.json()["next_run_at"]                       # computed on save
    jobs = client.get("/api/jobs").json()
    assert [j["id"] for j in jobs] == [jid]
    r = client.patch(f"/api/jobs/{jid}", json={"enabled": False})
    assert r.json()["enabled"] is False
    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    assert client.get("/api/jobs").json() == []


def test_create_rejects_bad_cron(app_client):
    client, _ = app_client
    r = client.post("/api/jobs", json={**_DRAFT, "cron": "not a cron"})
    assert r.status_code == 400


def test_patch_cron_revalidates_and_recomputes(app_client):
    client, _ = app_client
    jid = client.post("/api/jobs", json=_DRAFT).json()["id"]
    before = client.get("/api/jobs").json()[0]["next_run_at"]
    r = client.patch(f"/api/jobs/{jid}", json={"cron": "bad"})
    assert r.status_code == 400
    r = client.patch(f"/api/jobs/{jid}", json={"cron": "0 9 * * *"})
    assert r.status_code == 200
    assert r.json()["next_run_at"] != before


def test_run_now_fires_even_when_paused(app_client, monkeypatch):
    client, _ = app_client
    created = []

    class _T:
        id = 7
        identifier = "T-7"

    monkeypatch.setattr("aiforge_core.tickets.store.create",
                        lambda **kw: created.append(kw) or _T())
    jid = client.post("/api/jobs", json=_DRAFT).json()["id"]
    client.patch(f"/api/jobs/{jid}", json={"enabled": False})
    r = client.post(f"/api/jobs/{jid}/run-now")
    assert r.status_code == 200
    assert len(created) == 1
    assert created[0]["metadata"]["source"] == "scheduled_job"


def test_create_script_job_writes_script_and_runs_on_fire(app_client, tmp_path):
    client, _ = app_client
    marker = tmp_path / "job-ran.txt"
    r = client.post("/api/jobs/script", json={
        "name": "nightly cleanup", "cron": "0 9 * * *",
        "script": f"echo hi > {marker}"})
    assert r.status_code == 201
    body = r.json()
    assert body["kind"] == "script"
    assert body["script_path"]
    assert body["script_path"].endswith(".sh")
    # run-now shares the fire path → the script actually executes (no ticket).
    r = client.post(f"/api/jobs/{body['id']}/run-now")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    # _fire_script runs the script in a daemon thread (so a slow script never
    # blocks the tick/HTTP), so "ok" means DISPATCHED, not finished — poll for
    # the marker instead of asserting synchronously (else a loaded box races).
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()


def test_delete_script_job_keeps_script_file_for_reuse(app_client):
    client, _ = app_client
    r = client.post("/api/jobs/script", json={
        "name": "temp job", "cron": "0 9 * * *", "script": "true"})
    path = r.json()["script_path"]
    assert os.path.isfile(path)
    jid = r.json()["id"]
    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    # Deleting the job removes the schedule (the row), not the user's
    # script content — left on disk so it can be reused for a new job.
    assert os.path.isfile(path)


def test_deleted_job_row_is_gone_so_it_can_never_fire_again(app_client):
    client, _ = app_client
    jid = client.post("/api/jobs/script", json={
        "name": "temp job", "cron": "0 9 * * *", "script": "true"}).json()["id"]
    assert client.delete(f"/api/jobs/{jid}").status_code == 200
    assert client.get("/api/jobs").json() == []


def test_delete_ticket_job_has_no_script_to_clean_up(app_client):
    client, _ = app_client
    jid = client.post("/api/jobs", json=_DRAFT).json()["id"]
    assert client.delete(f"/api/jobs/{jid}").status_code == 200  # no crash


def test_create_script_job_rejects_bad_cron(app_client):
    client, _ = app_client
    r = client.post("/api/jobs/script",
                    json={"name": "x", "cron": "nope", "script": "true"})
    assert r.status_code == 400


def test_missing_job_404s(app_client):
    client, _ = app_client
    assert client.patch("/api/jobs/999", json={}).status_code == 404
    assert client.delete("/api/jobs/999").status_code == 404
    assert client.post("/api/jobs/999/run-now").status_code == 404


def test_create_impossible_date_cron_is_400_not_500(app_client):
    client, _ = app_client
    # "0 0 31 2 *" passes croniter.is_valid but is unschedulable — must be a
    # graceful 400, never a 500 from next_runs crashing.
    r = client.post("/api/jobs", json={**_DRAFT, "cron": "0 0 31 2 *"})
    assert r.status_code == 400
    assert client.get("/api/jobs").json() == []          # nothing persisted


def test_preview_impossible_date_cron_is_ok_false_not_500(app_client, monkeypatch):
    client, _ = app_client
    monkeypatch.setattr("aiforge_core.llm.client.complete", lambda *a, **k: json.dumps(
        {**_DRAFT, "cron": "0 0 31 2 *"}))
    r = client.post("/api/jobs/preview", json={"instructions": "feb 31 nonsense"})
    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_patch_cannot_blank_required_field(app_client):
    client, _ = app_client
    jid = client.post("/api/jobs", json=_DRAFT).json()["id"]
    assert client.patch(f"/api/jobs/{jid}",
                        json={"ticket_title": "  "}).status_code == 400
    assert client.patch(f"/api/jobs/{jid}",
                        json={"name": ""}).status_code == 400


def test_create_accepts_cron_alias(app_client):
    client, _ = app_client
    # "@daily" is croniter-valid but shorter than a 5-field cron — the old
    # min_length=9 heuristic wrongly 422'd it.
    r = client.post("/api/jobs", json={**_DRAFT, "cron": "@daily"})
    assert r.status_code == 201


def test_degrades_gracefully_when_croniter_absent(app_client, monkeypatch):
    """A deployment missing croniter must not 500-crash the Jobs page:
    the list still works, preview returns a friendly error, create/run-now
    return an actionable 503 instead of an opaque ModuleNotFoundError."""
    client, _ = app_client
    from aiforge_core.jobs import parse as jobs_parse
    monkeypatch.setattr(jobs_parse, "CRONITER_AVAILABLE", False)

    # list still renders (store-only, no croniter) → page doesn't break
    assert client.get("/api/jobs").status_code == 200

    # preview → friendly, not a 500
    p = client.post("/api/jobs/preview", json={"instructions": "every day 8am"})
    assert p.status_code == 200
    assert p.json()["ok"] is False
    assert "croniter" in p.json()["error"].lower()

    # create → actionable 503
    c = client.post("/api/jobs", json=_DRAFT)
    assert c.status_code == 503
    assert "croniter" in c.json()["detail"].lower()


def test_parse_module_imports_without_croniter():
    """parse.py must load even when croniter is absent (guarded import)."""
    from aiforge_core.jobs import parse as jobs_parse
    assert hasattr(jobs_parse, "CRONITER_AVAILABLE")
    assert jobs_parse.human_schedule("0 8 * * *")  # no-croniter path works
