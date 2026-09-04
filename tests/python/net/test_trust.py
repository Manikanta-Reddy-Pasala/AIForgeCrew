"""Pinning a self-hosted endpoint's certificate, instead of not verifying it.

Every "skip TLS verify" path in the product used to hand back a context with
``CERT_NONE`` and hostname checking off. Scoped to one endpoint, deliberate,
documented — and still no verification at all, so anything on the path could be
that host and the client would never know. The flag meant "this host is
self-signed"; it now does what that should always have meant: trust THAT
certificate, and keep verifying.
"""
from __future__ import annotations

import ssl

import pytest

from aiforge_core.net import trust
from tests.python.tls_pin_fixture import (
    another_self_signed_pem,
    self_signed_pem,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_TLS_NO_TOFU", raising=False)
    return tmp_path


# ── storing a pin ───────────────────────────────────────────────────────────

def test_a_pin_is_stored_owner_only_next_to_the_credentials(_isolated):
    pem = self_signed_pem()
    fp = trust.store("jira.internal", pem)
    p = trust.pin_path("jira.internal")
    assert p.is_file()
    assert p.read_text() == pem
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    assert p.parent.name == "trusted_certs"
    assert p.parent.parent.name == "security"
    assert fp.count(":") == 31       # SHA-256, colon-separated


def test_the_pin_round_trips():
    pem = self_signed_pem()
    trust.store("jira.internal", pem)
    assert trust.pinned_pem("jira.internal") == pem


def test_an_unknown_host_has_no_pin():
    assert trust.pinned_pem("nothing.pinned") == ""


@pytest.mark.parametrize("host", ["../../etc/passwd", "a/b", "", "x" * 300,
                                  "host name", "héllo.example"])
def test_a_hostname_that_is_not_one_builds_no_path(host):
    """The host comes from configuration, but a FILENAME built from anything
    network-adjacent is worth pinning down rather than trusting."""
    assert trust.pin_path(host) is None
    assert trust.store(host, self_signed_pem()) == ""


def test_the_fingerprint_is_the_one_every_tool_prints():
    fp = trust.fingerprint(self_signed_pem())
    assert len(fp.split(":")) == 32
    assert fp == fp.upper()
    assert trust.fingerprint("not a certificate") == ""


# ── using a pin ─────────────────────────────────────────────────────────────

def test_a_pinned_context_verifies_and_trusts_only_that_cert(monkeypatch):
    pem = self_signed_pem()
    trust.store("jira.internal", pem)
    ctx = trust.context_for_pin("jira.internal")
    assert ctx is not None
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    subjects = [dict(i for rdn in c["subject"] for i in rdn)
                for c in ctx.get_ca_certs()]
    assert [s["commonName"] for s in subjects] == ["pinned-test-cert"]


def test_no_pin_and_no_reachable_host_returns_none(monkeypatch):
    """The caller then falls back to ordinary verification — the connection
    fails on the certificate rather than opening unverified."""
    monkeypatch.setattr(trust, "fetch", lambda host, port=443: "")
    assert trust.context_for_pin("unreachable.internal") is None


def test_first_use_fetches_and_records(monkeypatch):
    """The operator marking an endpoint self-signed IS the consent, so the
    first connection pins it. The window is one connection, not every
    connection forever."""
    pem = self_signed_pem()
    monkeypatch.setattr(trust, "fetch", lambda host, port=443: pem)
    assert trust.pinned_pem("new.internal") == ""
    assert trust.ensure_pinned("new.internal") == pem
    assert trust.pinned_pem("new.internal") == pem


def test_a_pinned_host_is_never_re_fetched(monkeypatch):
    pem = self_signed_pem()
    trust.store("jira.internal", pem)

    def _boom(host, port=443):
        raise AssertionError("a pinned host must not be fetched again")

    monkeypatch.setattr(trust, "fetch", _boom)
    assert trust.ensure_pinned("jira.internal") == pem


def test_trust_on_first_use_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_TLS_NO_TOFU", "1")
    monkeypatch.setattr(trust, "fetch",
                        lambda host, port=443: self_signed_pem())
    assert trust.ensure_pinned("new.internal") == ""
    assert trust.pinned_pem("new.internal") == ""


# ── rotation, removal, visibility ───────────────────────────────────────────

def test_a_changed_certificate_is_loud_and_then_accepted(caplog):
    """An internal CA re-issues. Refusing the new certificate would strand the
    operator with no way back except deleting a file they do not know about —
    so it is written, and the log line is what makes it visible. Same event as
    ssh warning about a changed host key, and it deserves the same volume."""
    import logging

    trust.store("jira.internal", self_signed_pem())
    other = another_self_signed_pem()
    with caplog.at_level(logging.WARNING, logger="aiforge.trust"):
        trust.store("jira.internal", other)
    assert "CHANGED" in caplog.text
    assert trust.pinned_pem("jira.internal") == other


def test_forget_removes_a_pin():
    trust.store("jira.internal", self_signed_pem())
    assert trust.forget("jira.internal") is True
    assert trust.pinned_pem("jira.internal") == ""
    assert trust.forget("jira.internal") is False


def test_listing_answers_what_do_we_trust():
    """A question nobody could ask while the answer was "whatever answers"."""
    trust.store("jira.internal", self_signed_pem())
    trust.store("gitlab.internal", self_signed_pem())
    rows = trust.listing()
    assert {r["host"] for r in rows} == {"jira.internal", "gitlab.internal"}
    assert all(r["fingerprint"].count(":") == 31 for r in rows)


def test_an_empty_pem_is_not_stored():
    assert trust.store("jira.internal", "   ") == ""
    assert trust.pinned_pem("jira.internal") == ""


def test_a_broken_pin_file_falls_back_rather_than_raising(_isolated,
                                                          monkeypatch):
    """A truncated or hand-edited PEM must not take down every call to that
    host with a stack trace."""
    trust.store("jira.internal", "-----BEGIN CERTIFICATE-----\nnope\n")
    monkeypatch.setattr(trust, "fetch", lambda host, port=443: "")
    assert trust.context_for_pin("jira.internal") is None
