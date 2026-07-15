"""Integration config routes — Confluence / Jira / GitLab / Email.

Read / persist / test each integration's connection settings (secrets kept
write-only). Extracted from api.py (APIRouter split).
"""
from __future__ import annotations

import os

from fastapi import APIRouter
from pydantic import BaseModel

from aiforge_core.api._shared import env_truthy as _env_truthy

router = APIRouter()


class _ConfluenceCfg(BaseModel):
    base_url: str | None = None
    token: str | None = None       # write-only; omitted on read
    user: str | None = None
    insecure_tls: bool | None = None
    default_space: str | None = None   # auto-applied when a call omits `space`


@router.get("/api/integrations/confluence")
def integrations_confluence_get() -> dict:
    """Current Confluence settings (token masked). Reflects env override."""
    from aiforge_core.config import integrations
    stored = integrations.get("confluence")
    env_token = bool(os.environ.get("CONFLUENCE_TOKEN"))
    return {
        "base_url": os.environ.get("CONFLUENCE_BASE_URL") or stored.get("base_url", ""),
        "user": os.environ.get("CONFLUENCE_USER") or stored.get("user", ""),
        "insecure_tls": bool(stored.get("insecure_tls")),
        "has_token": env_token or bool(stored.get("token")),
        "default_space": os.environ.get("CONFLUENCE_DEFAULT_SPACE")
        or stored.get("default_space", ""),
        "env_managed": bool(os.environ.get("CONFLUENCE_BASE_URL") or env_token),
    }


@router.put("/api/integrations/confluence")
def integrations_confluence_set(body: _ConfluenceCfg) -> dict:
    """Persist Confluence settings. An empty/omitted token keeps the existing
    one (so re-saving the form doesn't wipe the secret)."""
    from aiforge_core.config import integrations
    patch: dict = {}
    if body.base_url is not None:
        patch["base_url"] = body.base_url.strip().rstrip("/")
    if body.user is not None:
        patch["user"] = body.user.strip()
    if body.insecure_tls is not None:
        patch["insecure_tls"] = bool(body.insecure_tls)
    if body.default_space is not None:
        patch["default_space"] = body.default_space.strip()
    if body.token:                       # only overwrite when a new token is given
        patch["token"] = body.token.strip()
    integrations.set_("confluence", patch)
    return integrations_confluence_get()


@router.post("/api/integrations/confluence/test")
def integrations_confluence_test() -> dict:
    """Live connectivity + auth check against the configured Confluence."""
    from aiforge_core.runtime.tools.confluence import confluence_test
    return confluence_test()


class _JiraCfg(BaseModel):
    base_url: str | None = None
    token: str | None = None       # write-only; omitted on read
    user: str | None = None
    insecure_tls: bool | None = None
    default_project: str | None = None  # auto-applied when a call omits `project`


@router.get("/api/integrations/jira")
def integrations_jira_get() -> dict:
    """Current Jira settings (token masked). Reflects env override."""
    from aiforge_core.config import integrations
    stored = integrations.get("jira")
    env_token = bool(os.environ.get("JIRA_TOKEN"))
    return {
        "base_url": os.environ.get("JIRA_BASE_URL") or stored.get("base_url", ""),
        "user": os.environ.get("JIRA_USER") or stored.get("user", ""),
        "insecure_tls": bool(stored.get("insecure_tls")),
        "has_token": env_token or bool(stored.get("token")),
        "default_project": os.environ.get("JIRA_DEFAULT_PROJECT")
        or stored.get("default_project", ""),
        "env_managed": bool(os.environ.get("JIRA_BASE_URL") or env_token),
    }


@router.put("/api/integrations/jira")
def integrations_jira_set(body: _JiraCfg) -> dict:
    """Persist Jira settings. An empty/omitted token keeps the existing one
    (so re-saving the form doesn't wipe the secret)."""
    from aiforge_core.config import integrations
    patch: dict = {}
    if body.base_url is not None:
        patch["base_url"] = body.base_url.strip().rstrip("/")
    if body.user is not None:
        patch["user"] = body.user.strip()
    if body.insecure_tls is not None:
        patch["insecure_tls"] = bool(body.insecure_tls)
    if body.default_project is not None:
        patch["default_project"] = body.default_project.strip()
    if body.token:                       # only overwrite when a new token is given
        patch["token"] = body.token.strip()
    integrations.set_("jira", patch)
    return integrations_jira_get()


@router.post("/api/integrations/jira/test")
def integrations_jira_test() -> dict:
    """Live connectivity + auth check against the configured Jira."""
    from aiforge_core.runtime.tools.jira import jira_test
    return jira_test()


class _GitlabCfg(BaseModel):
    base_url: str | None = None
    token: str | None = None       # write-only; omitted on read
    project: str | None = None     # default project (id or "group/proj")
    oauth: bool | None = None      # token sent as Bearer instead of PRIVATE-TOKEN
    insecure_tls: bool | None = None


@router.get("/api/integrations/gitlab")
def integrations_gitlab_get() -> dict:
    """Current GitLab settings (token masked). Reflects env override."""
    from aiforge_core.config import integrations
    stored = integrations.get("gitlab")
    env_token = bool(os.environ.get("GITLAB_TOKEN"))
    return {
        "base_url": os.environ.get("GITLAB_BASE_URL") or stored.get("base_url", ""),
        "project": os.environ.get("GITLAB_PROJECT") or stored.get("project", ""),
        "oauth": bool(stored.get("oauth")),
        "insecure_tls": bool(stored.get("insecure_tls")),
        "has_token": env_token or bool(stored.get("token")),
        "env_managed": bool(os.environ.get("GITLAB_BASE_URL") or env_token),
    }


@router.put("/api/integrations/gitlab")
def integrations_gitlab_set(body: _GitlabCfg) -> dict:
    """Persist GitLab settings. An empty/omitted token keeps the existing one
    (so re-saving the form doesn't wipe the secret)."""
    from aiforge_core.config import integrations
    patch: dict = {}
    if body.base_url is not None:
        patch["base_url"] = body.base_url.strip().rstrip("/")
    if body.project is not None:
        patch["project"] = body.project.strip()
    if body.oauth is not None:
        patch["oauth"] = bool(body.oauth)
    if body.insecure_tls is not None:
        patch["insecure_tls"] = bool(body.insecure_tls)
    if body.token:                       # only overwrite when a new token is given
        patch["token"] = body.token.strip()
    integrations.set_("gitlab", patch)
    return integrations_gitlab_get()


@router.post("/api/integrations/gitlab/test")
def integrations_gitlab_test() -> dict:
    """Live connectivity + auth check against the configured GitLab."""
    from aiforge_core.runtime.tools.gitlab import gitlab_test
    return gitlab_test()


class _EmailCfg(BaseModel):
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None   # write-only; omitted on read
    smtp_from: str | None = None
    smtp_starttls: bool | None = None
    imap_host: str | None = None
    imap_port: int | None = None
    imap_user: str | None = None
    imap_password: str | None = None   # write-only; omitted on read
    imap_ssl: bool | None = None


@router.get("/api/integrations/email")
def integrations_email_get() -> dict:
    """Current Email (SMTP/IMAP) settings (passwords masked). Reflects env
    override — an env-set host/password wins over the stored value."""
    from aiforge_core.config import integrations
    stored = integrations.get("email")
    env_smtp_pw = bool(os.environ.get("AIFORGE_SMTP_PASSWORD"))
    env_imap_pw = bool(os.environ.get("AIFORGE_IMAP_PASSWORD"))
    env_managed = bool(
        os.environ.get("AIFORGE_SMTP_HOST") or os.environ.get("AIFORGE_IMAP_HOST")
        or env_smtp_pw or env_imap_pw)
    return {
        "smtp_host": os.environ.get("AIFORGE_SMTP_HOST") or stored.get("smtp_host", ""),
        "smtp_port": int(os.environ.get("AIFORGE_SMTP_PORT") or stored.get("smtp_port") or 587),
        "smtp_user": os.environ.get("AIFORGE_SMTP_USER") or stored.get("smtp_user", ""),
        "smtp_from": os.environ.get("AIFORGE_SMTP_FROM") or stored.get("smtp_from", ""),
        "smtp_starttls": _env_truthy("AIFORGE_SMTP_STARTTLS")
                         if os.environ.get("AIFORGE_SMTP_STARTTLS") else bool(stored.get("smtp_starttls", True)),
        "imap_host": os.environ.get("AIFORGE_IMAP_HOST") or stored.get("imap_host", ""),
        "imap_port": int(os.environ.get("AIFORGE_IMAP_PORT") or stored.get("imap_port") or 993),
        "imap_user": os.environ.get("AIFORGE_IMAP_USER") or stored.get("imap_user", ""),
        "imap_ssl": _env_truthy("AIFORGE_IMAP_SSL")
                    if os.environ.get("AIFORGE_IMAP_SSL") else bool(stored.get("imap_ssl", True)),
        "has_smtp_password": env_smtp_pw or bool(stored.get("smtp_password")),
        "has_imap_password": env_imap_pw or bool(stored.get("imap_password")),
        "env_managed": env_managed,
    }


@router.put("/api/integrations/email")
def integrations_email_set(body: _EmailCfg) -> dict:
    """Persist Email (SMTP/IMAP) settings. An empty/omitted password keeps the
    existing one (so re-saving the form doesn't wipe the secret)."""
    from aiforge_core.config import integrations
    patch: dict = {}
    for f in ("smtp_host", "smtp_user", "smtp_from", "imap_host", "imap_user"):
        v = getattr(body, f)
        if v is not None:
            patch[f] = v.strip()
    for f in ("smtp_port", "imap_port"):
        v = getattr(body, f)
        if v is not None:
            patch[f] = int(v)
    for f in ("smtp_starttls", "imap_ssl"):
        v = getattr(body, f)
        if v is not None:
            patch[f] = bool(v)
    if body.smtp_password:               # only overwrite when a new secret is given
        patch["smtp_password"] = body.smtp_password
    if body.imap_password:
        patch["imap_password"] = body.imap_password
    integrations.set_("email", patch)
    return integrations_email_get()


@router.post("/api/integrations/email/test")
def integrations_email_test() -> dict:
    """Live connectivity + auth check against the configured SMTP/IMAP."""
    from aiforge_core.runtime.tools.email_tool import email_test
    return email_test()
