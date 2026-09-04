"""Integration settings: Confluence, Jira, GitLab and Email.

Every one of these forms holds a secret, and the same two rules apply to all
four. A secret is WRITE-ONLY: a read reports only whether one is set, never
the value. And an empty or omitted secret KEEPS the stored one — otherwise
re-saving the form (which the UI does with a masked field) silently wipes the
token and the integration stops working with no visible cause.

The third rule is environment precedence: an env-set host or token wins over
the stored value, and the read says so with ``env_managed`` so the UI can
explain why editing the field changes nothing.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiforge_core.api.routes import integrations as ig


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(ig.router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def store(monkeypatch):
    from aiforge_core.config import integrations
    rows: dict = {"confluence": {}, "jira": {}, "gitlab": {}, "email": {}}
    monkeypatch.setattr(integrations, "get", lambda name: dict(rows.get(name, {})))
    monkeypatch.setattr(integrations, "set_",
                        lambda name, patch: rows.setdefault(name, {}).update(patch))
    for var in ("CONFLUENCE_BASE_URL", "CONFLUENCE_TOKEN", "CONFLUENCE_USER",
                "CONFLUENCE_DEFAULT_SPACE", "JIRA_BASE_URL", "JIRA_TOKEN",
                "JIRA_USER", "JIRA_DEFAULT_PROJECT", "GITLAB_BASE_URL",
                "GITLAB_TOKEN", "GITLAB_PROJECT", "AIFORGE_SMTP_HOST",
                "AIFORGE_SMTP_PORT", "AIFORGE_SMTP_USER", "AIFORGE_SMTP_FROM",
                "AIFORGE_SMTP_PASSWORD", "AIFORGE_SMTP_STARTTLS",
                "AIFORGE_IMAP_HOST", "AIFORGE_IMAP_PORT", "AIFORGE_IMAP_USER",
                "AIFORGE_IMAP_PASSWORD", "AIFORGE_IMAP_SSL"):
        monkeypatch.delenv(var, raising=False)
    return rows


# ─── Confluence ────────────────────────────────────────────────────────


def test_confluence_settings_are_saved_and_read_back(client, store):
    client.put("/api/integrations/confluence",
               json={"base_url": " https://wiki.internal/ ", "user": " ada ",
                     "token": " sk-secret ", "default_space": " ENG ",
                     "insecure_tls": True})
    body = client.get("/api/integrations/confluence").json()
    assert body["base_url"] == "https://wiki.internal"      # trimmed, no slash
    assert body["user"] == "ada"
    assert body["default_space"] == "ENG"
    assert body["insecure_tls"] is True
    assert body["has_token"] is True
    assert "sk-secret" not in str(body)                     # write-only


def test_re_saving_the_form_keeps_the_token(client, store):
    """The UI masks the field, so a blank token means "unchanged" — treating
    it as a clear silently breaks the integration."""
    client.put("/api/integrations/confluence",
               json={"base_url": "https://wiki", "token": "sk-secret"})
    client.put("/api/integrations/confluence",
               json={"base_url": "https://wiki2", "token": ""})
    assert store["confluence"]["token"] == "sk-secret"
    assert client.get("/api/integrations/confluence").json()["has_token"] is True


def test_an_env_token_counts_as_configured(client, store, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_TOKEN", "sk-env")
    body = client.get("/api/integrations/confluence").json()
    assert body["has_token"] is True
    assert body["env_managed"] is True


def test_an_env_base_url_wins_over_the_stored_one(client, store, monkeypatch):
    store["confluence"]["base_url"] = "https://stored"
    monkeypatch.setenv("CONFLUENCE_BASE_URL", "https://from-env")
    body = client.get("/api/integrations/confluence").json()
    assert body["base_url"] == "https://from-env"
    assert body["env_managed"] is True


def test_an_unconfigured_confluence_reads_as_empty(client, store):
    body = client.get("/api/integrations/confluence").json()
    assert body == {"base_url": "", "user": "", "insecure_tls": False,
                    "has_token": False, "default_space": "",
                    "env_managed": False}


def test_the_confluence_test_button_calls_the_client(client, monkeypatch):
    import aiforge_core.runtime.tools.confluence as conf
    monkeypatch.setattr(conf, "confluence_test",
                        lambda: {"ok": True, "auth": "bearer"})
    assert client.post("/api/integrations/confluence/test").json()["auth"] == "bearer"


# ─── Jira ──────────────────────────────────────────────────────────────


def test_jira_settings_are_saved_and_read_back(client, store):
    client.put("/api/integrations/jira",
               json={"base_url": "https://jira.internal/", "user": " ada ",
                     "token": "sk-jira", "default_project": " ENG ",
                     "insecure_tls": True})
    body = client.get("/api/integrations/jira").json()
    assert body["base_url"] == "https://jira.internal"
    assert body["default_project"] == "ENG"
    assert body["has_token"] is True
    assert "sk-jira" not in str(body)


def test_re_saving_jira_keeps_the_token(client, store):
    client.put("/api/integrations/jira", json={"token": "sk-jira"})
    client.put("/api/integrations/jira", json={"user": "bo"})
    assert store["jira"]["token"] == "sk-jira"


def test_an_env_managed_jira_is_flagged(client, store, monkeypatch):
    monkeypatch.setenv("JIRA_BASE_URL", "https://from-env")
    monkeypatch.setenv("JIRA_USER", "env-user")
    body = client.get("/api/integrations/jira").json()
    assert body["env_managed"] is True
    assert body["user"] == "env-user"


def test_the_jira_test_button_calls_the_client(client, monkeypatch):
    import aiforge_core.runtime.tools.jira as jira
    monkeypatch.setattr(jira, "jira_test", lambda: {"ok": False, "hint": "use Bearer"})
    assert client.post("/api/integrations/jira/test").json()["hint"] == "use Bearer"


# ─── GitLab ────────────────────────────────────────────────────────────


def test_gitlab_settings_are_saved_and_read_back(client, store):
    client.put("/api/integrations/gitlab",
               json={"base_url": "https://gitlab.internal/", "project": " grp/p ",
                     "token": "glpat-x", "oauth": True, "insecure_tls": True})
    body = client.get("/api/integrations/gitlab").json()
    assert body["base_url"] == "https://gitlab.internal"
    assert body["project"] == "grp/p"
    assert body["oauth"] is True
    assert body["has_token"] is True
    assert "glpat-x" not in str(body)


def test_re_saving_gitlab_keeps_the_token(client, store):
    client.put("/api/integrations/gitlab", json={"token": "glpat-x"})
    client.put("/api/integrations/gitlab", json={"project": "grp/other"})
    assert store["gitlab"]["token"] == "glpat-x"


def test_an_env_gitlab_project_wins(client, store, monkeypatch):
    store["gitlab"]["project"] = "stored/p"
    monkeypatch.setenv("GITLAB_PROJECT", "env/p")
    assert client.get("/api/integrations/gitlab").json()["project"] == "env/p"


def test_the_gitlab_test_button_calls_the_client(client, monkeypatch):
    import aiforge_core.runtime.tools.gitlab as gl
    monkeypatch.setattr(gl, "gitlab_test", lambda: {"ok": True, "user": "ada"})
    assert client.post("/api/integrations/gitlab/test").json()["user"] == "ada"


# ─── Email ─────────────────────────────────────────────────────────────


def test_email_settings_are_saved_and_read_back(client, store):
    client.put("/api/integrations/email",
               json={"smtp_host": " smtp.internal ", "smtp_port": 25,
                     "smtp_user": " ada ", "smtp_from": " ada@corp ",
                     "smtp_password": "pw1", "smtp_starttls": False,
                     "imap_host": "imap.internal", "imap_port": 143,
                     "imap_user": "ada", "imap_password": "pw2",
                     "imap_ssl": False})
    body = client.get("/api/integrations/email").json()
    assert body["smtp_host"] == "smtp.internal"
    assert body["smtp_port"] == 25
    assert body["smtp_starttls"] is False
    assert body["imap_ssl"] is False
    assert body["has_smtp_password"] is True
    assert body["has_imap_password"] is True
    assert "pw1" not in str(body)
    assert "pw2" not in str(body)


def test_re_saving_email_keeps_both_passwords(client, store):
    client.put("/api/integrations/email",
               json={"smtp_password": "pw1", "imap_password": "pw2"})
    client.put("/api/integrations/email", json={"smtp_host": "new.host"})
    assert store["email"]["smtp_password"] == "pw1"
    assert store["email"]["imap_password"] == "pw2"


def test_the_default_ports_and_tls_settings(client, store):
    body = client.get("/api/integrations/email").json()
    assert body["smtp_port"] == 587
    assert body["imap_port"] == 993
    assert body["smtp_starttls"] is True
    assert body["imap_ssl"] is True


def test_env_ports_and_flags_win(client, store, monkeypatch):
    monkeypatch.setenv("AIFORGE_SMTP_PORT", "2525")
    monkeypatch.setenv("AIFORGE_SMTP_STARTTLS", "0")
    monkeypatch.setenv("AIFORGE_IMAP_SSL", "0")
    body = client.get("/api/integrations/email").json()
    assert body["smtp_port"] == 2525
    assert body["smtp_starttls"] is False
    assert body["imap_ssl"] is False


def test_an_env_password_counts_as_configured(client, store, monkeypatch):
    monkeypatch.setenv("AIFORGE_IMAP_PASSWORD", "env-pw")
    body = client.get("/api/integrations/email").json()
    assert body["has_imap_password"] is True
    assert body["env_managed"] is True


def test_the_email_test_button_calls_the_client(client, monkeypatch):
    import aiforge_core.runtime.tools.email_tool as et
    monkeypatch.setattr(et, "email_test", lambda: {"smtp": "ok", "imap": "ok"})
    assert client.post("/api/integrations/email/test").json()["smtp"] == "ok"
