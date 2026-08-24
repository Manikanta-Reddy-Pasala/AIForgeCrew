"""Email tool — SMTP send / IMAP read, mocked at the smtplib/imaplib layer.

Mirrors the Jira tool test shape: soft-fail contract when unconfigured, no
network client constructed unless config is present, and the write path
(email_send) defaults to the chat approval gate.
"""
from __future__ import annotations

import pytest

from aiforge_core.runtime.tools import email_tool as et


# ── config fixtures ───────────────────────────────────────────────────

@pytest.fixture
def smtp_cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_SMTP_HOST", "smtp.internal")
    monkeypatch.setenv("AIFORGE_SMTP_PORT", "587")
    monkeypatch.setenv("AIFORGE_SMTP_USER", "bot@company.com")
    monkeypatch.setenv("AIFORGE_SMTP_PASSWORD", "s3cr3t")
    monkeypatch.delenv("AIFORGE_SMTP_FROM", raising=False)
    monkeypatch.delenv("AIFORGE_SMTP_STARTTLS", raising=False)
    monkeypatch.delenv("AIFORGE_EMAIL_DISABLE", raising=False)


@pytest.fixture
def imap_cfg(monkeypatch):
    monkeypatch.setenv("AIFORGE_IMAP_HOST", "imap.internal")
    monkeypatch.setenv("AIFORGE_IMAP_PORT", "993")
    monkeypatch.setenv("AIFORGE_IMAP_USER", "bot@company.com")
    monkeypatch.setenv("AIFORGE_IMAP_PASSWORD", "s3cr3t")
    monkeypatch.delenv("AIFORGE_IMAP_SSL", raising=False)
    monkeypatch.delenv("AIFORGE_EMAIL_DISABLE", raising=False)


# ── fakes ─────────────────────────────────────────────────────────────

class _FakeSMTP:
    """Records calls; captures the EmailMessage handed to send_message."""
    instances: list = []

    def __init__(self, host, port=0, timeout=None):
        self.host = host
        self.port = port
        self.calls: list[str] = []
        self.sent = None
        self.to_addrs = None
        self.login_creds = None
        _FakeSMTP.instances.append(self)

    def ehlo(self, *a):
        self.calls.append("ehlo")

    def starttls(self, *a, **k):
        self.calls.append("starttls")

    def login(self, user, pw):
        self.calls.append("login")
        self.login_creds = (user, pw)

    def send_message(self, msg, from_addr=None, to_addrs=None):
        self.calls.append("send_message")
        self.sent = msg
        self.to_addrs = to_addrs

    def quit(self):
        self.calls.append("quit")


def _explode(*a, **k):  # any construction ⇒ boom (proves it was NOT called)
    raise AssertionError("network client must not be constructed when unconfigured")


class _FakeIMAP:
    """Minimal IMAP4_SSL stand-in returning a couple of fake messages."""
    last: "_FakeIMAP | None" = None

    def __init__(self, host, port=0):
        self.host = host
        self.port = port
        self.selected = None
        self.search_args = None
        _FakeIMAP.last = self
        # three fake RFC822 blobs, newest last (seq 1,2,3)
        self._store = {
            b"1": (b"From: Alice <alice@x.com>\r\nTo: bot@company.com\r\n"
                   b"Subject: Deploy runbook\r\nDate: Mon, 1 Jul 2026 10:00:00 +0000\r\n"
                   b"\r\nThe deploy steps are attached below.\r\n"),
            b"2": (b"From: Bob <bob@y.com>\r\nTo: bot@company.com\r\n"
                   b"Subject: Lunch?\r\nDate: Mon, 1 Jul 2026 11:00:00 +0000\r\n"
                   b"\r\nWanna grab lunch today.\r\n"),
            b"3": (b"From: Carol <carol@z.com>\r\nTo: bot@company.com\r\n"
                   b"Subject: Invoice\r\nDate: Mon, 1 Jul 2026 12:00:00 +0000\r\n"
                   b"\r\nPlease find the invoice details here.\r\n"),
        }

    def login(self, user, pw):
        self.login_creds = (user, pw)
        return ("OK", [b"logged in"])

    def select(self, folder):
        self.selected = folder
        return ("OK", [b"3"])

    def search(self, charset, *criteria):
        self.search_args = criteria
        return ("OK", [b"1 2 3"])

    def fetch(self, num, spec):
        key = num if isinstance(num, bytes) else str(num).encode()
        return ("OK", [(b"%s (RFC822)" % key, self._store[key]), b")"])

    def close(self):
        pass

    def logout(self):
        pass


# ── email_send ────────────────────────────────────────────────────────

def test_send_unconfigured_no_smtp_call(monkeypatch):
    monkeypatch.delenv("AIFORGE_SMTP_HOST", raising=False)
    monkeypatch.setattr("smtplib.SMTP", _explode)
    monkeypatch.setattr("smtplib.SMTP_SSL", _explode)
    out = et.email_send({"to": "a@b.com", "subject": "hi", "body": "x"})
    assert out["ok"] is False
    assert "not configured" in out["error"]


def test_send_starttls_and_login(smtp_cfg, monkeypatch):
    _FakeSMTP.instances.clear()
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    out = et.email_send({"to": ["a@b.com", "c@d.com"], "subject": "Report",
                         "body": "the body", "cc": "cc@e.com"})
    assert out["ok"] is True
    assert out["to"] == ["a@b.com", "c@d.com"]
    assert out["subject"] == "Report"
    srv = _FakeSMTP.instances[-1]
    assert "starttls" in srv.calls
    assert "login" in srv.calls
    assert srv.login_creds == ("bot@company.com", "s3cr3t")
    assert "send_message" in srv.calls
    msg = srv.sent
    assert msg["To"] == "a@b.com, c@d.com"
    assert msg["Subject"] == "Report"
    assert msg["From"] == "bot@company.com"
    # cc included as recipient (header) but bcc-style recipients passed explicitly
    assert "cc@e.com" in srv.to_addrs


def test_send_from_defaults_to_user(smtp_cfg, monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    et.email_send({"to": "a@b.com", "subject": "s", "body": "b"})
    assert _FakeSMTP.instances[-1].sent["From"] == "bot@company.com"


def test_send_requires_to(smtp_cfg, monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", _explode)
    monkeypatch.setattr("smtplib.SMTP_SSL", _explode)
    out = et.email_send({"subject": "s", "body": "b"})
    assert out["ok"] is False
    assert "to" in out["error"].lower()


# ── email_read ────────────────────────────────────────────────────────

def test_read_unconfigured_no_imap_call(monkeypatch):
    monkeypatch.delenv("AIFORGE_IMAP_HOST", raising=False)
    monkeypatch.setattr("imaplib.IMAP4_SSL", _explode)
    monkeypatch.setattr("imaplib.IMAP4", _explode)
    out = et.email_read({})
    assert out["ok"] is False
    assert "not configured" in out["error"]


def test_read_parses_messages(imap_cfg, monkeypatch):
    monkeypatch.setattr("imaplib.IMAP4_SSL", _FakeIMAP)
    out = et.email_read({"limit": 2})
    assert out["ok"] is True
    msgs = out["messages"]
    assert len(msgs) == 2                      # limit respected
    m0 = msgs[0]
    assert set(m0) >= {"uid", "from", "to", "subject", "date", "snippet"}
    # newest first (seq 3 = Carol)
    assert "Carol" in m0["from"]
    assert m0["subject"] == "Invoice"
    assert "invoice" in m0["snippet"].lower()


def test_read_query_filters_subject(imap_cfg, monkeypatch):
    monkeypatch.setattr("imaplib.IMAP4_SSL", _FakeIMAP)
    out = et.email_read({"query": "lunch", "limit": 10})
    assert out["ok"] is True
    assert len(out["messages"]) == 1
    assert out["messages"][0]["subject"] == "Lunch?"


def test_read_unseen_only_uses_imap_criteria(imap_cfg, monkeypatch):
    monkeypatch.setattr("imaplib.IMAP4_SSL", _FakeIMAP)
    et.email_read({"unseen_only": True})
    assert "UNSEEN" in _FakeIMAP.last.search_args


# ── master kill switch ────────────────────────────────────────────────

def test_disable_switch_blocks_both(smtp_cfg, imap_cfg, monkeypatch):
    monkeypatch.setenv("AIFORGE_EMAIL_DISABLE", "1")
    monkeypatch.setattr("smtplib.SMTP", _explode)
    monkeypatch.setattr("smtplib.SMTP_SSL", _explode)
    monkeypatch.setattr("imaplib.IMAP4_SSL", _explode)
    monkeypatch.setattr("imaplib.IMAP4", _explode)
    s = et.email_send({"to": "a@b.com", "subject": "x", "body": "y"})
    r = et.email_read({})
    assert s["ok"] is False
    assert "disabled" in s["error"].lower()
    assert r["ok"] is False
    assert "disabled" in r["error"].lower()


# ── tool wiring (mirror the jira wiring assertions) ───────────────────

def test_wired_into_chat_agent_tools():
    from aiforge_core.runtime import chat_agent
    assert "email_send" in chat_agent.TOOLS
    assert "email_read" in chat_agent.TOOLS


def test_wired_into_doer_function_tools():
    pytest.importorskip("google.adk")
    from aiforge_core.runtime import doer_tools
    names = {t.func.__name__ for t in doer_tools.adk_function_tools()}
    assert "email_send" in names
    assert "email_read" in names
    assert "email_send" in doer_tools.__all__
    assert "email_read" in doer_tools.__all__


def test_send_defaults_to_ask_policy(monkeypatch):
    from aiforge_core.runtime.tools import tool_policy
    monkeypatch.delenv("AIFORGE_TOOL_POLICY", raising=False)
    monkeypatch.delenv("AIFORGE_CHAT_TOOL_POLICY", raising=False)
    assert tool_policy.decide("email_send", {})["policy"] == tool_policy.ASK
    assert tool_policy.decide("email_read", {})["policy"] == tool_policy.ALLOW


# ── UI-persisted config store (mirror the jira/gitlab store tests) ────

_ENV_KEYS = (
    "AIFORGE_SMTP_HOST", "AIFORGE_SMTP_PORT", "AIFORGE_SMTP_USER",
    "AIFORGE_SMTP_PASSWORD", "AIFORGE_SMTP_FROM", "AIFORGE_SMTP_STARTTLS",
    "AIFORGE_IMAP_HOST", "AIFORGE_IMAP_PORT", "AIFORGE_IMAP_USER",
    "AIFORGE_IMAP_PASSWORD", "AIFORGE_IMAP_SSL", "AIFORGE_EMAIL_DISABLE",
)


def _clear_env(monkeypatch):
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


def test_reads_stored_smtp_config_when_env_absent(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import integrations
    integrations.set_("email", {
        "smtp_host": "smtp.stored", "smtp_port": 2525, "smtp_user": "u@stored",
        "smtp_password": "stored-pw", "smtp_from": "from@stored",
        "smtp_starttls": False,
    })
    _FakeSMTP.instances.clear()
    monkeypatch.setattr("smtplib.SMTP_SSL", _FakeSMTP)   # starttls False ⇒ SMTP_SSL
    out = et.email_send({"to": "a@b.com", "subject": "s", "body": "b"})
    assert out["ok"] is True
    srv = _FakeSMTP.instances[-1]
    assert srv.host == "smtp.stored"
    assert srv.port == 2525
    assert srv.login_creds == ("u@stored", "stored-pw")
    assert srv.sent["From"] == "from@stored"


def test_reads_stored_imap_config_when_env_absent(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import integrations
    integrations.set_("email", {"imap_host": "imap.stored", "imap_port": 1993,
                                 "imap_user": "u@stored", "imap_password": "pw"})
    monkeypatch.setattr("imaplib.IMAP4_SSL", _FakeIMAP)
    out = et.email_read({"limit": 1})
    assert out["ok"] is True
    assert _FakeIMAP.last.host == "imap.stored"
    assert _FakeIMAP.last.port == 1993


def test_env_wins_over_stored(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    from aiforge_core.config import integrations
    integrations.set_("email", {"smtp_host": "smtp.stored", "smtp_user": "stored",
                                "smtp_password": "stored-pw"})
    monkeypatch.setenv("AIFORGE_SMTP_HOST", "smtp.env")
    monkeypatch.setenv("AIFORGE_SMTP_USER", "env-user")
    monkeypatch.setenv("AIFORGE_SMTP_PASSWORD", "env-pw")
    _FakeSMTP.instances.clear()
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)       # starttls default True
    et.email_send({"to": "a@b.com", "subject": "s", "body": "b"})
    srv = _FakeSMTP.instances[-1]
    assert srv.host == "smtp.env"
    assert srv.login_creds == ("env-user", "env-pw")


def test_email_test_unconfigured(tmp_path, monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    out = et.email_test()
    assert out["ok"] is False
    assert out["error"] == "email_not_configured"


def test_email_test_smtp_only_ok(tmp_path, smtp_cfg, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))   # no stored imap host
    monkeypatch.delenv("AIFORGE_IMAP_HOST", raising=False)
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    out = et.email_test()
    assert out["ok"] is True
    assert out["smtp"]["ok"] is True
    assert out["smtp"]["host"] == "smtp.internal"
    assert out["imap"] is None
