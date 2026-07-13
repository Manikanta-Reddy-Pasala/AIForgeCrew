#!/usr/bin/env python3
"""Microsoft Teams / Graph probe — a standalone test tool (no AIForge deps).

Point it at your Azure AD app and verify you can READ and LISTEN to Teams
messages BEFORE we integrate. Only `requests` is needed (already in the venv).

────────────────────────────────────────────────────────────────────────────
AZURE SETUP (one time)
  1. portal.azure.com → Azure Active Directory → App registrations → New.
  2. Copy the Application (client) ID and Directory (tenant) ID.
  3. Certificates & secrets → New client secret → copy the VALUE.
  4. API permissions → Microsoft Graph → add, then "Grant admin consent":
       APP-ONLY (client-credentials, server reads channels):
           Team.ReadBasic.All, Channel.ReadBasic.All,
           ChannelMessage.Read.All            (Application perms)
       DELEGATED (device-code, reads YOUR chats + channels):
           Chat.Read, ChannelMessage.Read.All,
           Team.ReadBasic.All, User.Read, offline_access   (Delegated)
     For device-code add: Authentication → Allow public client flows → Yes.

CREDENTIALS (env, or a KEY=VALUE file passed with --env-file)
     App-only (channels)  : TEAMS_TENANT_ID + TEAMS_CLIENT_ID + TEAMS_CLIENT_SECRET
     Delegated (your chats): NOTHING required — if TEAMS_CLIENT_ID / TEAMS_TENANT_ID
        are unset, the probe uses Microsoft's public Azure-CLI client id and the
        'organizations' authority, so `login` / `password` work with no app
        registration at all. (Set them only to use your own app.)

USAGE
  App-only (server, channels):
     python scripts/teams_probe.py teams
     python scripts/teams_probe.py channels <team_id>
     python scripts/teams_probe.py messages <team_id> <channel_id> [--top 20]
     python scripts/teams_probe.py poll     <team_id> <channel_id> [--interval 5]

  Delegated (your personal chats — sign in once with device code):
     python scripts/teams_probe.py login          # prints a code to enter
     python scripts/teams_probe.py chats

  Username + password (ROPC — no browser, no client secret). Still needs a
  public-client app registration for TEAMS_CLIENT_ID; FAILS if the account has
  MFA (use `login` then). Creds via env, never on the command line:
     export TEAMS_TENANT_ID=... TEAMS_CLIENT_ID=...
     export TEAMS_USERNAME=you@org.com TEAMS_PASSWORD=...
     python scripts/teams_probe.py password       # caches a token, then:
     python scripts/teams_probe.py chats
     python scripts/teams_probe.py chat-messages <chat_id> [--top 20]
     python scripts/teams_probe.py chat-poll      <chat_id> [--interval 5]

The 'poll' commands are the "listen" test — they print each NEW message as it
lands (Graph has no push without a public webhook; polling is the simple path).
────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import requests

GRAPH = "https://graph.microsoft.com/v1.0"
_TOKEN_CACHE = os.path.expanduser("~/.aiforge/teams_token.json")


# ── credentials ──────────────────────────────────────────────────────────────
def _load_env_file(path: str) -> None:
    if not path or not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            k, _, v = line.partition("=")
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


def _cfg(name: str, required: bool = True) -> str:
    val = os.environ.get(name, "").strip()
    if required and not val:
        sys.exit(f"error: {name} is not set (export it or use --env-file)")
    return val


# Microsoft's own public client — the Azure CLI first-party app. It's
# pre-consented for delegated Graph in virtually every tenant, so the
# device-code and password flows work WITHOUT registering your own app.
# (App-only client-credentials still needs YOUR registration + secret.)
_AZ_CLI_CLIENT = "04b07795-8ddb-461a-bbee-02f9e1bf7b46"


def _client_id() -> str:
    """TEAMS_CLIENT_ID if set, else the Azure CLI public client id."""
    cid = os.environ.get("TEAMS_CLIENT_ID", "").strip()
    if not cid:
        cid = _AZ_CLI_CLIENT
        print(f"note: TEAMS_CLIENT_ID unset — using the Azure CLI public client "
              f"({cid}); no app registration needed", file=sys.stderr)
    return cid


def _authority() -> str:
    """Tenant segment for the login URL. TEAMS_TENANT_ID if set, else
    'organizations' — work/school sign-in without knowing the tenant GUID
    (the flow resolves it from your account)."""
    return os.environ.get("TEAMS_TENANT_ID", "").strip() or "organizations"


# ── auth: app-only (client credentials) ──────────────────────────────────────
def _app_token() -> str:
    tenant = _cfg("TEAMS_TENANT_ID")
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": _cfg("TEAMS_CLIENT_ID"),
            "client_secret": _cfg("TEAMS_CLIENT_SECRET"),
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=30,
    )
    if r.status_code != 200:
        sys.exit(f"token error {r.status_code}: {r.text}")
    return r.json()["access_token"]


# ── auth: delegated (device code, cached + refreshed) ────────────────────────
_DELEGATED_SCOPES = ("ChannelMessage.Read.All Chat.Read Team.ReadBasic.All "
                     "User.Read offline_access")


def _device_login() -> dict:
    tenant = _authority()
    client = _client_id()
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode",
        data={"client_id": client, "scope": _DELEGATED_SCOPES}, timeout=30)
    if r.status_code != 200:
        sys.exit(f"devicecode error {r.status_code}: {r.text}")
    d = r.json()
    print(f"\n  {d['message']}\n")               # "go to …/device and enter CODE"
    interval = int(d.get("interval", 5))
    tok_url = f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
    while True:
        time.sleep(interval)
        t = requests.post(tok_url, data={
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "client_id": client, "device_code": d["device_code"]}, timeout=30)
        j = t.json()
        if t.status_code == 200:
            os.makedirs(os.path.dirname(_TOKEN_CACHE), exist_ok=True)
            with open(_TOKEN_CACHE, "w", encoding="utf-8") as fh:
                json.dump(j, fh)
            print("signed in — token cached at", _TOKEN_CACHE)
            return j
        if j.get("error") == "authorization_pending":
            continue
        sys.exit(f"login failed: {j.get('error_description', j)}")


def _password_login() -> dict:
    """ROPC — sign in with a raw username + password (no browser, no secret).

    Caveats (Microsoft's, not ours): needs a public-client app registration
    (Authentication → Allow public client flows → Yes), works ONLY for
    cloud/managed accounts, and FAILS if the account has MFA or is federated.
    If it errors with interaction_required/AADSTS50076, use `login` (device
    code) instead. Creds come from env (TEAMS_USERNAME / TEAMS_PASSWORD) so they
    never land in shell history — never paste them into chat."""
    tenant = _authority()
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "grant_type": "password",
            "client_id": _client_id(),
            "username": _cfg("TEAMS_USERNAME"),
            "password": _cfg("TEAMS_PASSWORD"),
            "scope": _DELEGATED_SCOPES,
        }, timeout=30)
    j = r.json()
    if r.status_code != 200:
        hint = ""
        if "50076" in r.text or "50079" in r.text or "interaction_required" in r.text:
            hint = ("\nhint: the account requires MFA — ROPC can't do that. "
                    "Run `teams_probe.py login` (device code) instead.")
        sys.exit(f"password login failed {r.status_code}: "
                 f"{j.get('error_description', r.text)}{hint}")
    os.makedirs(os.path.dirname(_TOKEN_CACHE), exist_ok=True)
    with open(_TOKEN_CACHE, "w", encoding="utf-8") as fh:
        json.dump(j, fh)
    print("signed in (password) — token cached at", _TOKEN_CACHE)
    return j


def _delegated_token() -> str:
    if not os.path.isfile(_TOKEN_CACHE):
        sys.exit("not signed in — run:  teams_probe.py login  (or: password)")
    with open(_TOKEN_CACHE, encoding="utf-8") as fh:
        tok = json.load(fh)
    # try the access token; on 401 the caller refreshes
    return tok.get("access_token", "")


def _refresh_delegated() -> str:
    with open(_TOKEN_CACHE, encoding="utf-8") as fh:
        tok = json.load(fh)
    tenant = _authority()
    r = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={"grant_type": "refresh_token", "client_id": _client_id(),
              "refresh_token": tok.get("refresh_token", ""),
              "scope": _DELEGATED_SCOPES}, timeout=30)
    if r.status_code != 200:
        sys.exit("refresh failed — run 'login' again: " + r.text)
    j = r.json()
    with open(_TOKEN_CACHE, "w", encoding="utf-8") as fh:
        json.dump(j, fh)
    return j["access_token"]


# ── graph helper (auto-refresh delegated on 401) ─────────────────────────────
def _get(path: str, token: str, delegated: bool = False) -> dict:
    url = path if path.startswith("http") else f"{GRAPH}{path}"
    r = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
    if r.status_code == 401 and delegated:
        token = _refresh_delegated()
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"},
                         timeout=30)
    if r.status_code != 200:
        sys.exit(f"graph {r.status_code} on {url}: {r.text}")
    return r.json()


def _body_text(msg: dict) -> str:
    body = (msg.get("body") or {}).get("content", "") or ""
    # crude HTML strip for readability in a terminal
    import re
    return re.sub(r"<[^>]+>", "", body).strip()


def _fmt(msg: dict) -> str:
    frm = ((msg.get("from") or {}).get("user") or {}).get("displayName", "?")
    when = msg.get("createdDateTime", "")
    return f"[{when}] {frm}: {_body_text(msg)[:400]}"


# ── commands ─────────────────────────────────────────────────────────────────
def cmd_teams(_a):
    tok = _app_token()
    for t in _get("/teams?$select=id,displayName&$top=50", tok).get("value", []):
        print(f"{t['id']}  {t.get('displayName', '')}")


def cmd_channels(a):
    tok = _app_token()
    for c in _get(f"/teams/{a.team_id}/channels", tok).get("value", []):
        print(f"{c['id']}  {c.get('displayName', '')}")


def cmd_messages(a):
    tok = _app_token()
    data = _get(f"/teams/{a.team_id}/channels/{a.channel_id}/messages"
                f"?$top={a.top}", tok)
    for m in data.get("value", []):
        print(_fmt(m))


def cmd_poll(a):
    tok = _app_token()
    seen: set[str] = set()
    base = f"/teams/{a.team_id}/channels/{a.channel_id}/messages?$top=20"
    print(f"listening (every {a.interval}s) — Ctrl-C to stop\n")
    first = True
    while True:
        for m in reversed(_get(base, tok).get("value", [])):
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            if not first:                        # skip the backlog on first pass
                print(_fmt(m))
        first = False
        time.sleep(a.interval)


def cmd_login(_a):
    _device_login()


def cmd_password(_a):
    _password_login()


def cmd_chats(_a):
    tok = _delegated_token()
    for c in _get("/me/chats?$top=50", tok, delegated=True).get("value", []):
        topic = c.get("topic") or c.get("chatType") or "(no topic)"
        print(f"{c['id']}  {topic}")


def cmd_chat_messages(a):
    tok = _delegated_token()
    data = _get(f"/me/chats/{a.chat_id}/messages?$top={a.top}", tok,
                delegated=True)
    for m in data.get("value", []):
        print(_fmt(m))


def cmd_chat_poll(a):
    tok = _delegated_token()
    seen: set[str] = set()
    base = f"/me/chats/{a.chat_id}/messages?$top=20"
    print(f"listening (every {a.interval}s) — Ctrl-C to stop\n")
    first = True
    while True:
        for m in reversed(_get(base, tok, delegated=True).get("value", [])):
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            if not first:
                print(_fmt(m))
        first = False
        time.sleep(a.interval)


def main(argv=None):
    p = argparse.ArgumentParser(description="Microsoft Teams / Graph probe")
    p.add_argument("--env-file", help="KEY=VALUE creds file to load")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("teams").set_defaults(func=cmd_teams)
    c = sub.add_parser("channels"); c.add_argument("team_id")
    c.set_defaults(func=cmd_channels)
    m = sub.add_parser("messages")
    m.add_argument("team_id"); m.add_argument("channel_id")
    m.add_argument("--top", type=int, default=20); m.set_defaults(func=cmd_messages)
    po = sub.add_parser("poll")
    po.add_argument("team_id"); po.add_argument("channel_id")
    po.add_argument("--interval", type=int, default=5); po.set_defaults(func=cmd_poll)

    sub.add_parser("login").set_defaults(func=cmd_login)
    sub.add_parser("password").set_defaults(func=cmd_password)
    sub.add_parser("chats").set_defaults(func=cmd_chats)
    cm = sub.add_parser("chat-messages"); cm.add_argument("chat_id")
    cm.add_argument("--top", type=int, default=20)
    cm.set_defaults(func=cmd_chat_messages)
    cp = sub.add_parser("chat-poll"); cp.add_argument("chat_id")
    cp.add_argument("--interval", type=int, default=5)
    cp.set_defaults(func=cmd_chat_poll)

    a = p.parse_args(argv)
    _load_env_file(a.env_file or "")
    try:
        a.func(a)
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
