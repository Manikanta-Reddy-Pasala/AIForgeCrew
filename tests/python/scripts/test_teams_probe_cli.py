"""teams_probe.py — the sign-in flows and the CLI commands.

The auth half is where a wrong answer wastes a person's afternoon, so the
tests pin what the script tells them: device-code polling waits through
``authorization_pending`` and caches the token on success, and a ROPC
password login that trips MFA says "use device code" rather than repeating an
opaque AADSTS number.

The command half is thin — Graph in, formatted lines out — but the pollers
must skip the BACKLOG on their first pass, or every start floods the terminal
with old messages.
"""
from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[3] / "scripts" / "teams_probe.py"


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


@pytest.fixture
def tp(tmp_path, monkeypatch):
    """The script as a module, with a scratch token cache and no real sleep."""
    for k in ("TEAMS_CLIENT_ID", "TEAMS_TENANT_ID", "TEAMS_CLIENT_SECRET",
              "TEAMS_USERNAME", "TEAMS_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    spec = importlib.util.spec_from_file_location("teams_probe_cli_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["teams_probe_cli_under_test"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_TOKEN_CACHE", str(tmp_path / "cache" / "tok.json"))
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    yield mod
    sys.modules.pop("teams_probe_cli_under_test", None)


@pytest.fixture
def http(tp, monkeypatch):
    """Queue POST/GET replies; record what was sent."""
    state: dict = {"posts": [], "gets": [], "post_replies": [], "get_replies": []}

    def _post(url, data=None, timeout=None, **kw):
        state["posts"].append({"url": url, "data": data or {}})
        return state["post_replies"].pop(0) if state["post_replies"] else _Resp()

    def _get(url, headers=None, timeout=None, **kw):
        state["gets"].append({"url": url, "headers": headers or {}})
        return state["get_replies"].pop(0) if state["get_replies"] else _Resp()
    monkeypatch.setattr(tp.requests, "post", _post)
    monkeypatch.setattr(tp.requests, "get", _get)
    return state


# ─── device-code login ─────────────────────────────────────────────────


def test_device_login_waits_through_pending_then_caches(tp, http, capsys):
    http["post_replies"] = [
        _Resp(payload={"message": "go to microsoft.com/devicelogin, code ABC",
                       "device_code": "dc", "interval": 1}),
        _Resp(status=400, payload={"error": "authorization_pending"}),
        _Resp(payload={"access_token": "at", "refresh_token": "rt"}),
    ]
    out = tp._device_login()
    assert out["access_token"] == "at"
    assert json.load(open(tp._TOKEN_CACHE))["refresh_token"] == "rt"
    assert "code ABC" in capsys.readouterr().out


def test_a_failed_device_code_request_exits(tp, http):
    http["post_replies"] = [_Resp(status=400, text="bad client")]
    with pytest.raises(SystemExit) as ei:
        tp._device_login()
    assert "devicecode error 400" in str(ei.value)


def test_a_rejected_device_login_exits_with_the_reason(tp, http):
    http["post_replies"] = [
        _Resp(payload={"message": "go", "device_code": "dc"}),
        _Resp(status=400, payload={"error": "expired_token",
                                   "error_description": "code expired"}),
    ]
    with pytest.raises(SystemExit) as ei:
        tp._device_login()
    assert "code expired" in str(ei.value)


def test_the_azure_cli_client_is_used_when_none_is_registered(tp, http, capsys):
    http["post_replies"] = [
        _Resp(payload={"message": "go", "device_code": "dc"}),
        _Resp(payload={"access_token": "at"})]
    tp._device_login()
    assert http["posts"][0]["data"]["client_id"] == tp._AZ_CLI_CLIENT
    assert "no app registration needed" in capsys.readouterr().err


def test_a_registered_client_and_tenant_are_used(tp, http, monkeypatch):
    monkeypatch.setenv("TEAMS_CLIENT_ID", "my-app")
    monkeypatch.setenv("TEAMS_TENANT_ID", "tenant-guid")
    http["post_replies"] = [
        _Resp(payload={"message": "go", "device_code": "dc"}),
        _Resp(payload={"access_token": "at"})]
    tp._device_login()
    assert http["posts"][0]["data"]["client_id"] == "my-app"
    assert "tenant-guid" in http["posts"][0]["url"]


# ─── password (ROPC) login ─────────────────────────────────────────────


def test_a_password_login_caches_its_token(tp, http, monkeypatch):
    monkeypatch.setenv("TEAMS_USERNAME", "ada@corp")
    monkeypatch.setenv("TEAMS_PASSWORD", "hunter2")
    http["post_replies"] = [_Resp(payload={"access_token": "at"})]
    assert tp._password_login()["access_token"] == "at"
    sent = http["posts"][0]["data"]
    assert sent["grant_type"] == "password"
    assert sent["username"] == "ada@corp"


def test_an_mfa_account_is_told_to_use_device_code(tp, http, monkeypatch):
    """ROPC cannot do MFA — an opaque AADSTS number helps nobody."""
    monkeypatch.setenv("TEAMS_USERNAME", "ada@corp")
    monkeypatch.setenv("TEAMS_PASSWORD", "hunter2")
    http["post_replies"] = [_Resp(status=400, payload={
        "error_description": "AADSTS50076: MFA required"},
        text="AADSTS50076 interaction_required")]
    with pytest.raises(SystemExit) as ei:
        tp._password_login()
    assert "device code" in str(ei.value)


def test_an_ordinary_password_failure_has_no_mfa_hint(tp, http, monkeypatch):
    monkeypatch.setenv("TEAMS_USERNAME", "ada@corp")
    monkeypatch.setenv("TEAMS_PASSWORD", "wrong")
    http["post_replies"] = [_Resp(status=401,
                                  payload={"error_description": "bad password"},
                                  text="AADSTS50126")]
    with pytest.raises(SystemExit) as ei:
        tp._password_login()
    assert "bad password" in str(ei.value)
    assert "device code" not in str(ei.value)


def test_missing_credentials_exit_before_any_request(tp, http):
    with pytest.raises(SystemExit) as ei:
        tp._password_login()
    assert "TEAMS_USERNAME is not set" in str(ei.value)
    assert http["posts"] == []


# ─── refreshing ────────────────────────────────────────────────────────


def _cache(tp, payload):
    import os
    os.makedirs(Path(tp._TOKEN_CACHE).parent, exist_ok=True)
    with open(tp._TOKEN_CACHE, "w") as fh:
        json.dump(payload, fh)


def test_a_refresh_rewrites_the_cache(tp, http):
    _cache(tp, {"access_token": "old", "refresh_token": "rt"})
    http["post_replies"] = [_Resp(payload={"access_token": "new",
                                           "refresh_token": "rt2"})]
    assert tp._refresh_delegated() == "new"
    assert json.load(open(tp._TOKEN_CACHE))["refresh_token"] == "rt2"
    assert http["posts"][0]["data"]["grant_type"] == "refresh_token"


def test_a_failed_refresh_says_to_sign_in_again(tp, http):
    _cache(tp, {"refresh_token": "stale"})
    http["post_replies"] = [_Resp(status=400, text="expired")]
    with pytest.raises(SystemExit) as ei:
        tp._refresh_delegated()
    assert "run 'login' again" in str(ei.value)


def test_an_expired_delegated_call_refreshes_once_and_retries(tp, http, monkeypatch):
    _cache(tp, {"access_token": "old", "refresh_token": "rt"})
    http["get_replies"] = [_Resp(status=401, text="expired"),
                           _Resp(payload={"value": [{"id": "1"}]})]
    monkeypatch.setattr(tp, "_refresh_delegated", lambda: "new")
    assert tp._get("/me/chats", "old", delegated=True) == {"value": [{"id": "1"}]}
    assert http["gets"][1]["headers"]["Authorization"] == "Bearer new"


def test_an_app_only_401_is_not_refreshed(tp, http):
    http["get_replies"] = [_Resp(status=401, text="denied")]
    with pytest.raises(SystemExit) as ei:
        tp._get("/teams", "tok")
    assert "graph 401" in str(ei.value)


# ─── commands ──────────────────────────────────────────────────────────


@pytest.fixture
def signed_in(tp, monkeypatch):
    monkeypatch.setattr(tp, "_app_token", lambda: "app-token")
    monkeypatch.setattr(tp, "_delegated_token", lambda: "user-token")
    pages: list = []
    monkeypatch.setattr(tp, "_get",
                        lambda path, token, delegated=False: pages.pop(0))
    return pages


def _msg(mid, who="Ada", body="<p>hello</p>"):
    return {"id": mid, "from": {"user": {"displayName": who}},
            "createdDateTime": "2026-01-01T00:00Z", "body": {"content": body}}


def test_teams_are_listed(tp, signed_in, capsys):
    signed_in.append({"value": [{"id": "t1", "displayName": "Engineering"}]})
    tp.cmd_teams(None)
    assert "t1  Engineering" in capsys.readouterr().out


def test_channels_are_listed(tp, signed_in, capsys):
    signed_in.append({"value": [{"id": "c1", "displayName": "General"}]})
    tp.cmd_channels(types.SimpleNamespace(team_id="t1"))
    assert "c1  General" in capsys.readouterr().out


def test_channel_messages_are_formatted(tp, signed_in, capsys):
    signed_in.append({"value": [_msg("m1")]})
    tp.cmd_messages(types.SimpleNamespace(team_id="t1", channel_id="c1", top=5))
    out = capsys.readouterr().out
    assert "Ada: hello" in out
    assert "<p>" not in out       # html stripped


def test_chats_are_listed_with_a_fallback_label(tp, signed_in, capsys):
    signed_in.append({"value": [{"id": "ch1", "topic": "Standup"},
                                {"id": "ch2", "chatType": "oneOnOne"},
                                {"id": "ch3"}]})
    tp.cmd_chats(None)
    out = capsys.readouterr().out
    assert "ch1  Standup" in out
    assert "ch2  oneOnOne" in out
    assert "ch3  (no topic)" in out


def test_chat_messages_are_formatted(tp, signed_in, capsys):
    signed_in.append({"value": [_msg("m1", who="Bo", body="hi there")]})
    tp.cmd_chat_messages(types.SimpleNamespace(chat_id="ch1", top=5))
    assert "Bo: hi there" in capsys.readouterr().out


def test_a_message_with_no_sender_still_formats(tp):
    assert "?: body" in tp._fmt({"body": {"content": "body"}})


def test_a_long_message_is_truncated(tp):
    assert len(tp._fmt(_msg("m", body="x" * 900))) < 500


# ─── polling ───────────────────────────────────────────────────────────


class _Stop(Exception):
    pass


def test_the_channel_poller_skips_the_backlog_then_prints_new(tp, monkeypatch,
                                                              capsys):
    monkeypatch.setattr(tp, "_app_token", lambda: "t")
    pages = [{"value": [_msg("old", body="old news")]},
             {"value": [_msg("new", body="fresh"), _msg("old", body="old news")]}]

    def _get(path, token, delegated=False):
        if not pages:
            raise _Stop
        return pages.pop(0)
    monkeypatch.setattr(tp, "_get", _get)
    args = types.SimpleNamespace(team_id="t", channel_id="c", interval=0)
    with pytest.raises(_Stop):
        tp.cmd_poll(args)
    out = capsys.readouterr().out
    assert "fresh" in out
    assert "old news" not in out


def test_the_chat_poller_skips_the_backlog_too(tp, monkeypatch, capsys):
    monkeypatch.setattr(tp, "_delegated_token", lambda: "t")
    pages = [{"value": [_msg("old", body="old news")]},
             {"value": [_msg("new", body="fresh")]}]

    def _get(path, token, delegated=False):
        if not pages:
            raise _Stop
        return pages.pop(0)
    monkeypatch.setattr(tp, "_get", _get)
    args = types.SimpleNamespace(chat_id="ch1", interval=0)
    with pytest.raises(_Stop):
        tp.cmd_chat_poll(args)
    out = capsys.readouterr().out
    assert "fresh" in out
    assert "old news" not in out


# ─── the CLI ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("argv,fn", [
    (["teams"], "cmd_teams"),
    (["channels", "t1"], "cmd_channels"),
    (["messages", "t1", "c1"], "cmd_messages"),
    (["poll", "t1", "c1"], "cmd_poll"),
    (["login"], "cmd_login"),
    (["password"], "cmd_password"),
    (["chats"], "cmd_chats"),
    (["chat-messages", "ch1"], "cmd_chat_messages"),
    (["chat-poll", "ch1"], "cmd_chat_poll"),
])
def test_every_subcommand_is_wired(tp, monkeypatch, argv, fn):
    called: list = []
    monkeypatch.setattr(tp, fn, lambda a: called.append(a))
    assert tp.main(argv) == 0
    assert len(called) == 1


def test_the_login_commands_delegate_to_the_flows(tp, monkeypatch):
    calls: list = []
    monkeypatch.setattr(tp, "_device_login", lambda: calls.append("device"))
    monkeypatch.setattr(tp, "_password_login", lambda: calls.append("password"))
    tp.cmd_login(None)
    tp.cmd_password(None)
    assert calls == ["device", "password"]


def test_default_paging_sizes(tp, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(tp, "cmd_messages", lambda a: seen.update(top=a.top))
    tp.main(["messages", "t1", "c1"])
    assert seen["top"] == 20
    monkeypatch.setattr(tp, "cmd_poll", lambda a: seen.update(interval=a.interval))
    tp.main(["poll", "t1", "c1"])
    assert seen["interval"] == 5


def test_an_env_file_is_loaded_before_the_command(tp, tmp_path, monkeypatch):
    env = tmp_path / "creds.env"
    env.write_text('# comment\n\nTEAMS_TENANT_ID="tenant-1"\nEMPTY=\n')
    monkeypatch.delenv("TEAMS_TENANT_ID", raising=False)
    seen: dict = {}
    monkeypatch.setattr(tp, "cmd_teams",
                        lambda a: seen.update(tenant=tp.os.environ.get("TEAMS_TENANT_ID")))
    tp.main(["--env-file", str(env), "teams"])
    assert seen["tenant"] == "tenant-1"


def test_the_env_file_never_overrides_the_real_environment(tp, tmp_path, monkeypatch):
    env = tmp_path / "creds.env"
    env.write_text("TEAMS_TENANT_ID=from-file\n")
    monkeypatch.setenv("TEAMS_TENANT_ID", "from-shell")
    tp._load_env_file(str(env))
    assert tp.os.environ["TEAMS_TENANT_ID"] == "from-shell"


def test_a_missing_env_file_is_ignored(tp, tmp_path):
    tp._load_env_file(str(tmp_path / "gone.env"))
    tp._load_env_file("")


def test_ctrl_c_exits_cleanly(tp, monkeypatch, capsys):
    monkeypatch.setattr(tp, "cmd_teams",
                        lambda a: (_ for _ in ()).throw(KeyboardInterrupt))
    assert tp.main(["teams"]) == 0
    assert "stopped." in capsys.readouterr().out
