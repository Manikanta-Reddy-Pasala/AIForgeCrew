"""/api/jobs — the end of a job, over HTTP.

The Jobs page creates DELIBERATE jobs, so unlike chat's `schedule_task` these
default to no expiry; `until` is how the page asks for one. Deleting closes
through the same door everything else does: learning kept, row gone.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_JOBS_DB_PATH", str(tmp_path / "jobs.db"))
    monkeypatch.setenv("AIFORGE_DB_PATH", str(tmp_path / "api.db"))
    from fastapi import FastAPI

    from aiforge_core.api.routes import jobs as r_jobs
    app = FastAPI()
    app.include_router(r_jobs.router)
    return TestClient(app)


@pytest.fixture
def captured(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr("aiforge_core.memory.md_store.capture",
                        lambda kind, text, **kw: seen.append(text))
    return seen


def _body(**over):
    b = {"name": "nightly digest", "cron": "0 8 * * *",
         "ticket_title": "digest", "ticket_body": "summarise yesterday"}
    b.update(over)
    return b


def test_a_job_from_the_jobs_page_runs_until_cancelled(client):
    """No `until` here means forever — the Jobs page is not the forgotten-loop
    case that the two-hour chat default exists for."""
    r = client.post("/api/jobs", json=_body())
    assert r.status_code == 201
    assert r.json()["expires_at"] is None


def test_until_sets_the_end(client):
    r = client.post("/api/jobs", json=_body(until="2d"))
    assert r.status_code == 201
    assert r.json()["expires_at"]


def test_an_unreadable_until_is_a_400_not_a_job_that_never_ends(client):
    r = client.post("/api/jobs", json=_body(until="soonish"))
    assert r.status_code == 400
    assert "until" in r.json()["detail"]
    assert client.get("/api/jobs").json() == []


def test_patch_can_extend_or_remove_the_end(client):
    job_id = client.post("/api/jobs", json=_body(until="2h")).json()["id"]
    ext = client.patch(f"/api/jobs/{job_id}", json={"until": "5d"}).json()
    assert ext["expires_at"]
    forever = client.patch(f"/api/jobs/{job_id}", json={"until": "forever"}).json()
    assert forever["expires_at"] is None


def test_delete_keeps_the_learning(client, captured):
    job_id = client.post("/api/jobs", json=_body()).json()["id"]
    r = client.delete(f"/api/jobs/{job_id}")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "learning_captured": True}
    assert captured
    assert "summarise yesterday" in captured[0]
    assert client.get("/api/jobs").json() == []


def test_deleting_a_job_that_is_not_there_is_still_a_404(client):
    assert client.delete("/api/jobs/9999").status_code == 404
