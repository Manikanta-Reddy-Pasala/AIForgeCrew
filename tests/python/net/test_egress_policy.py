"""Egress policy for DECLARED destinations — integrations, email, telemetry, MCP.

The web half (a model-composed URL) lives in test_egress_switches.py. This file
is about hosts the OPERATOR configured, where the question is not "where is it
going" but "should this content go out at all, and is anyone watching".

The gap that motivated it: approval is honoured in interactive chat, but an
unattended run has no approver — tool_gate degrades ASK to allow — so the
ticket pipeline and cron jobs could post to Jira or send mail with nobody
watching. That was never a deliberate decision, just the absence of one.
"""
from __future__ import annotations

import pytest

from aiforge_core.net import egress


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for var in ("AIFORGE_EGRESS_OFF", "AIFORGE_EGRESS_ALLOW_HOSTS",
                "AIFORGE_UNATTENDED_WRITES", "AIFORGE_UPLOAD_DISABLE",
                "AIFORGE_INTEGRATION_DISABLE", "AIFORGE_EMAIL_DISABLE",
                "AIFORGE_TELEMETRY_DISABLE", "AIFORGE_MCP_DISABLE"):
        monkeypatch.delenv(var, raising=False)


def _err(result):
    return (result or {}).get("error")


# ── reads pull data in; writes push content out ─────────────────────────────

def test_a_read_needs_no_human():
    assert egress.allow("integration", "https://jira.corp/i/1") is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_an_unattended_write_is_refused(method):
    assert _err(egress.allow("integration", "https://jira.corp/i/1",
                             method=method)) == "unattended_write_refused"


def test_an_attended_write_goes_through(monkeypatch):
    """With a human on the other end, approval already governs this."""
    monkeypatch.setattr(egress, "attended", lambda: True)
    assert egress.allow("email", "smtp://mail.corp", method="POST") is None


def test_the_operator_can_opt_unattended_runs_back_in(monkeypatch):
    monkeypatch.setenv("AIFORGE_UNATTENDED_WRITES", "1")
    assert egress.allow("integration", "https://jira.corp", method="POST") is None


def test_telemetry_is_exempt_from_the_attendance_rule():
    """A trace POST is observability, not a write on the user's behalf.
    Refusing it unattended would blind the pipeline — the runs whose traces
    matter most."""
    assert egress.allow("telemetry", "https://langfuse.corp",
                        method="POST") is None


# ── switches ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["integration", "email", "telemetry", "mcp"])
def test_the_master_switch_closes_every_class(monkeypatch, kind):
    monkeypatch.setenv("AIFORGE_EGRESS_OFF", "1")
    assert _err(egress.allow(kind, "https://x.corp")) == f"{kind}_egress_disabled"


@pytest.mark.parametrize("kind", ["integration", "email", "telemetry", "mcp"])
def test_each_class_has_its_own_switch(monkeypatch, kind):
    monkeypatch.setenv(f"AIFORGE_{kind.upper()}_DISABLE", "1")
    assert _err(egress.allow(kind, "https://x.corp")) == f"{kind}_egress_disabled"
    # and it does not close the others
    others = [k for k in ("integration", "email", "telemetry", "mcp") if k != kind]
    for other in others:
        assert egress.allow(other, "https://x.corp") is None


# ── destination allowlist ───────────────────────────────────────────────────

def test_no_allowlist_means_no_restriction():
    """Opt-in: an empty allowlist that denied everything would break every
    install the moment it shipped."""
    assert egress.host_allowed("https://anything.example") is True


def test_allowlist_matches_host_and_subdomain(monkeypatch):
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_HOSTS", "corp.example")
    assert egress.host_allowed("https://jira.corp.example/x") is True
    assert egress.host_allowed("https://corp.example/x") is True
    assert egress.host_allowed("https://notcorp.example/x") is False
    # a host smuggled into the path or fragment must not match
    assert egress.host_allowed("https://evil.example/#corp.example") is False


def test_allowlist_refuses_an_off_list_destination(monkeypatch):
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_HOSTS", "jira.corp")
    assert _err(egress.allow("integration", "https://evil.example")) == \
        "host_not_allowed"


# ── uploads are file CONTENT, not a sentence ────────────────────────────────

def test_upload_has_its_own_switch(monkeypatch):
    monkeypatch.setenv("AIFORGE_UNATTENDED_WRITES", "1")   # writes allowed…
    monkeypatch.setenv("AIFORGE_UPLOAD_DISABLE", "1")      # …uploads still not
    assert _err(egress.allow("integration", "https://c.corp",
                             upload=True)) == "upload_disabled"


def test_an_upload_counts_as_a_write_even_on_a_get():
    assert _err(egress.allow("integration", "https://c.corp", method="GET",
                             upload=True)) == "unattended_write_refused"


def test_an_unknown_class_is_a_hard_error():
    """Silently allowing an unrecognised class is how a new tool opts itself
    out of the policy."""
    with pytest.raises(ValueError):
        egress.allow("whatever", "https://x.corp")


# ── how each decision fails when its input is broken ────────────────────────
# These are the branches where a wrong default is a security decision, not a
# cosmetic one, so pin which way each one falls.

def test_a_malformed_url_is_not_on_the_allowlist(monkeypatch):
    """Fails CLOSED: an unparseable host cannot be matched, so it must not be
    treated as allowed."""
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_HOSTS", "corp.example")
    assert egress.host_allowed("http://[not-a-host") is False


def test_a_malformed_url_is_not_mistaken_for_a_search():
    """Fails OPEN in the other direction, and that is right: looks_like_search
    is a refusal, and refusing something we could not even parse would block
    ordinary fetches on a URL quirk. The gate above is what holds."""
    assert egress.looks_like_search("http://[not-a-host") is False


def test_attendance_is_false_when_the_session_layer_is_unavailable(monkeypatch):
    """Fails CLOSED: if we cannot tell whether a human is watching, assume not
    — the whole point of the check is that unattended is the risky state."""
    import builtins

    real_import = builtins.__import__

    def _boom(name, *a, **kw):
        if name.endswith("chat_cancel") or "chat_cancel" in name:
            raise ImportError("boom")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _boom)
    assert egress.attended() is False


def test_a_write_is_still_refused_when_attendance_cannot_be_determined(monkeypatch):
    monkeypatch.setattr(egress, "attended", lambda: False)
    assert _err(egress.allow("email", "smtp://mail.corp",
                             method="POST")) == "unattended_write_refused"
