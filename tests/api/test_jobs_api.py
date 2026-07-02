"""Jobs API — preview saves nothing; save re-validates; run-now shares
the fire path and works while paused."""
from __future__ import annotations

import importlib
import json

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
