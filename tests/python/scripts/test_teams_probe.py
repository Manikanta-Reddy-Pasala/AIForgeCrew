"""scripts/teams_probe.py — the Microsoft Graph / Teams probe. Was at 0%.

Every network call goes through `requests`, which is stubbed here, so the auth
branching, the 401-refresh path and the message formatting are exercised for
real without a tenant.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[3] / "scripts" / "teams_probe.py"


@pytest.fixture
def tp(tmp_path, monkeypatch):
    """Load the script as a module with a scratch token cache."""
    for k in ("TEAMS_CLIENT_ID", "TEAMS_TENANT_ID", "TEAMS_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    spec = importlib.util.spec_from_file_location("teams_probe_under_test", _SRC)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["teams_probe_under_test"] = mod
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_TOKEN_CACHE", str(tmp_path / "tok.json"))
    yield mod
    sys.modules.pop("teams_probe_under_test", None)


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


# ── env / config ──────────────────────────────────────────────────────


def test_env_file_loads_without_clobbering_existing_env(tp, tmp_path, monkeypatch):
    f = tmp_path / "e.env"
    f.write_text('# c\n\nNEW_KEY="v"\nTAKEN=from-file\n')
    monkeypatch.setenv("TAKEN", "from-shell")
    monkeypatch.delenv("NEW_KEY", raising=False)
    tp._load_env_file(str(f))
    import os
    assert os.environ["NEW_KEY"] == "v"
    assert os.environ["TAKEN"] == "from-shell"


def test_env_file_missing_is_a_no_op(tp, tmp_path):
    tp._load_env_file(str(tmp_path / "nope.env"))   # must not raise
    tp._load_env_file("")


def test_cfg_exits_when_a_required_value_is_absent(tp):
    with pytest.raises(SystemExit):
        tp._cfg("DEFINITELY_UNSET_VAR")


def test_cfg_returns_empty_when_not_required(tp):
    assert tp._cfg("DEFINITELY_UNSET_VAR", required=False) == ""


def test_client_id_falls_back_to_the_azure_cli_public_client(tp, capsys):
    assert tp._client_id() == tp._AZ_CLI_CLIENT
    assert "no app registration needed" in capsys.readouterr().err


def test_client_id_prefers_an_explicit_one(tp, monkeypatch):
    monkeypatch.setenv("TEAMS_CLIENT_ID", "my-app")
    assert tp._client_id() == "my-app"


def test_authority_defaults_to_organizations(tp, monkeypatch):
    assert tp._authority() == "organizations"
    monkeypatch.setenv("TEAMS_TENANT_ID", "tenant-guid")
    assert tp._authority() == "tenant-guid"


# ── app-only token ────────────────────────────────────────────────────


def test_app_token_returns_the_access_token(tp, monkeypatch):
    monkeypatch.setenv("TEAMS_TENANT_ID", "t")
    monkeypatch.setenv("TEAMS_CLIENT_ID", "c")
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "s")
    monkeypatch.setattr(tp.requests, "post",
                        lambda *a, **k: _Resp(200, {"access_token": "AT"}))
    assert tp._app_token() == "AT"


def test_app_token_exits_on_a_token_error(tp, monkeypatch):
    monkeypatch.setenv("TEAMS_TENANT_ID", "t")
    monkeypatch.setenv("TEAMS_CLIENT_ID", "c")
    monkeypatch.setenv("TEAMS_CLIENT_SECRET", "s")
    monkeypatch.setattr(tp.requests, "post",
                        lambda *a, **k: _Resp(401, text="bad secret"))
    with pytest.raises(SystemExit):
        tp._app_token()


# ── delegated token + refresh ─────────────────────────────────────────


def test_delegated_token_exits_when_not_signed_in(tp):
    with pytest.raises(SystemExit):
        tp._delegated_token()


def test_delegated_token_reads_the_cache(tp):
    Path(tp._TOKEN_CACHE).write_text(json.dumps({"access_token": "cached"}))
    assert tp._delegated_token() == "cached"


def test_refresh_writes_the_new_token_back_to_the_cache(tp, monkeypatch):
    Path(tp._TOKEN_CACHE).write_text(json.dumps({"refresh_token": "RT"}))
    monkeypatch.setattr(tp.requests, "post",
                        lambda *a, **k: _Resp(200, {"access_token": "NEW",
                                                    "refresh_token": "RT2"}))
    assert tp._refresh_delegated() == "NEW"
    assert json.loads(Path(tp._TOKEN_CACHE).read_text())["access_token"] == "NEW", \
        "the refreshed token must be persisted or every call re-refreshes"


def test_refresh_failure_exits_telling_you_to_log_in(tp, monkeypatch):
    Path(tp._TOKEN_CACHE).write_text(json.dumps({"refresh_token": "RT"}))
    monkeypatch.setattr(tp.requests, "post",
                        lambda *a, **k: _Resp(400, text="expired"))
    with pytest.raises(SystemExit):
        tp._refresh_delegated()


# ── graph GET, including the 401 auto-refresh ─────────────────────────


def test_get_builds_the_absolute_graph_url(tp, monkeypatch):
    seen = {}

    def _get(url, headers=None, timeout=None):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        return _Resp(200, {"value": []})
    monkeypatch.setattr(tp.requests, "get", _get)
    tp._get("/teams", "TOK")
    assert seen["url"] == tp.GRAPH + "/teams"
    assert seen["auth"] == "Bearer TOK"


def test_get_passes_an_absolute_url_through(tp, monkeypatch):
    seen = {}
    monkeypatch.setattr(tp.requests, "get",
                        lambda url, **k: seen.setdefault("url", url) and None
                        or _Resp(200, {}))
    tp._get("https://other/x", "TOK")
    assert seen["url"] == "https://other/x"


def test_get_refreshes_once_on_401_for_a_delegated_call(tp, monkeypatch):
    calls = {"n": 0}

    def _get(url, headers=None, timeout=None):
        calls["n"] += 1
        return _Resp(401) if calls["n"] == 1 else _Resp(200, {"ok": True})
    monkeypatch.setattr(tp.requests, "get", _get)
    monkeypatch.setattr(tp, "_refresh_delegated", lambda: "NEW")
    assert tp._get("/x", "OLD", delegated=True) == {"ok": True}
    assert calls["n"] == 2, "it must retry exactly once after refreshing"


def test_get_does_not_refresh_on_401_for_an_app_only_call(tp, monkeypatch):
    monkeypatch.setattr(tp.requests, "get", lambda *a, **k: _Resp(401, text="no"))
    monkeypatch.setattr(tp, "_refresh_delegated",
                        lambda: pytest.fail("app-only must not refresh"))
    with pytest.raises(SystemExit):
        tp._get("/x", "TOK", delegated=False)


def test_get_exits_on_a_non_200(tp, monkeypatch):
    monkeypatch.setattr(tp.requests, "get", lambda *a, **k: _Resp(500, text="boom"))
    with pytest.raises(SystemExit):
        tp._get("/x", "TOK")


# ── message formatting ────────────────────────────────────────────────


def test_body_text_strips_html(tp):
    msg = {"body": {"content": "<p>Hello <b>there</b></p>"}}
    assert tp._body_text(msg) == "Hello there"


def test_body_text_of_a_message_with_no_body(tp):
    assert tp._body_text({}) == ""


def test_fmt_renders_sender_time_and_body(tp):
    msg = {"from": {"user": {"displayName": "Ada"}},
           "createdDateTime": "2026-01-01T00:00:00Z",
           "body": {"content": "<p>hi</p>"}}
    assert tp._fmt(msg) == "[2026-01-01T00:00:00Z] Ada: hi"


def test_fmt_tolerates_a_message_with_no_sender(tp):
    assert tp._fmt({}).startswith("[] ?: ")


def test_fmt_truncates_a_long_body(tp):
    msg = {"body": {"content": "x" * 900}}
    assert len(tp._fmt(msg)) < 500


# ── commands ──────────────────────────────────────────────────────────


def test_cmd_teams_prints_id_and_name(tp, monkeypatch, capsys):
    monkeypatch.setattr(tp, "_app_token", lambda: "T")
    monkeypatch.setattr(tp, "_get", lambda p, t, **k: {
        "value": [{"id": "1", "displayName": "Eng"}]})
    tp.cmd_teams(None)
    assert "1  Eng" in capsys.readouterr().out


def test_cmd_channels_prints_id_and_name(tp, monkeypatch, capsys):
    monkeypatch.setattr(tp, "_app_token", lambda: "T")
    monkeypatch.setattr(tp, "_get", lambda p, t, **k: {
        "value": [{"id": "c1", "displayName": "general"}]})

    class _A:
        team_id = "t1"
    tp.cmd_channels(_A())
    assert "c1  general" in capsys.readouterr().out
