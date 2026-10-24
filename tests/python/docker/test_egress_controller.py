"""docker/egress-controller.py — the network allowlist's owner outside the box.

Entries become ANCHORED proxy patterns (a host and its subdomains, never a
look-alike), junk is refused, the first start seeds from the env and later
starts keep what Settings saved, every change is logged, and the HTTP API does
what the Settings card expects."""
from __future__ import annotations

import importlib.util
import json
import re
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def ctl(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "egress_controller", REPO / "docker" / "egress-controller.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "POLICY", str(tmp_path))
    monkeypatch.setattr(mod, "HOSTS", str(tmp_path / "hosts.json"))
    monkeypatch.setattr(mod, "ALLOW", str(tmp_path / "allow.txt"))
    monkeypatch.setattr(mod, "CHANGES", str(tmp_path / "changes.log"))
    monkeypatch.delenv("AIFORGE_EGRESS_ADMIN_TOKEN", raising=False)
    return mod


def _matches(lines, host):
    return any(re.search(p, host) for p in lines)


def test_entries_are_anchored_host_and_subdomains(ctl, tmp_path):
    ctl.save(["pypi.org", "https://Files.PythonHosted.org/simple/", "*.corp.example", "10.0.0.5"])
    pats = (tmp_path / "allow.txt").read_text().split()
    for ok in ("pypi.org", "www.pypi.org", "files.pythonhosted.org", "a.corp.example",
               "corp.example", "10.0.0.5"):
        assert _matches(pats, ok), ok
    for bad in ("evilpypi.org", "pypi.org.attacker.com", "pypiXorg", "10.0.0.50",
                "110.0.0.5", "example.com"):
        assert not _matches(pats, bad), bad


@pytest.mark.parametrize("junk", ["", "localhost", "a b.com", "-x.com", ".*", "x$.com",
                                  "http://", "a" * 64 + ".com", "127.1", "0177.0.0.1",
                                  "0x7f.1", "10.1"])
def test_junk_is_refused(ctl, junk):
    with pytest.raises(ValueError):
        ctl.save([junk])


def test_normalising_dedupes(ctl):
    assert ctl.save(["https://pypi.org:443/x", "PYPI.org", "pypi.org."]) == ["pypi.org"]


def test_first_start_seeds_from_env_then_settings_win(ctl, monkeypatch):
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_HOSTS", "artifactory.internal, jira.corp.example,bad host")
    ctl.seed()
    assert ctl.load() == ["artifactory.internal", "jira.corp.example"]
    ctl.save(["pypi.org"])
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_HOSTS", "other.example")
    ctl.seed()                                       # a restart keeps Settings' list
    assert ctl.load() == ["pypi.org"]


def test_an_empty_list_denies_everything(ctl, tmp_path):
    ctl.save([])
    assert (tmp_path / "allow.txt").read_text() == ""


def test_changes_are_logged(ctl):
    ctl.save(["a.example"], who="10.1.1.1")
    ctl.save(["b.example"], who="10.1.1.2")
    ch = ctl.changes()
    assert ch[0]["added"] == ["b.example"] and ch[0]["removed"] == ["a.example"]
    assert ch[0]["by"] == "10.1.1.2"


def test_a_corrupt_list_fails_closed_and_is_kept(ctl, tmp_path):
    (tmp_path / "hosts.json").write_text("{not json")
    (tmp_path / "allow.txt").write_text("^(.*\\.)?pypi\\.org$\n")
    ctl.seed()
    assert (tmp_path / "allow.txt").read_text() == ""              # nothing allowed
    assert (tmp_path / "hosts.json").read_text() == "{not json"   # left for a human


@pytest.fixture
def api(ctl):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), ctl.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    def call(method, path, body=None, token=None, raw=None):
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(base + path, method=method, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read()), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}"), dict(e.headers)
    yield call
    srv.shutdown()


def test_http_api(api):
    assert api("GET", "/api/hosts")[:2] == (200, {"hosts": [], "enforced": True})
    code, out, headers = api("PUT", "/api/hosts", {"hosts": ["pypi.org", "pypi.org"]})
    assert code == 200 and out["hosts"] == ["pypi.org"]
    assert not any(k.lower().startswith("access-control-") for k in headers)   # no CORS
    assert api("PUT", "/api/hosts", {"hosts": ["not a host"]})[0] == 400
    assert api("PUT", "/api/hosts", {"hosts": "pypi.org"})[0] == 400
    assert api("PUT", "/api/hosts", raw=b"[1,2]")[0] == 400        # not an object
    assert api("PUT", "/api/hosts", raw=b"{oops")[0] == 400
    assert api("GET", "/api/changes")[1]["changes"][0]["added"] == ["pypi.org"]
    assert api("GET", "/nope")[0] == 404


def test_open_by_default_token_when_configured(api, monkeypatch):
    """No token for now (the user's call); setting one later locks it."""
    assert api("PUT", "/api/hosts", {"hosts": ["a.example"]})[0] == 200
    monkeypatch.setenv("AIFORGE_EGRESS_ADMIN_TOKEN", "s3cret")
    assert api("PUT", "/api/hosts", {"hosts": ["evil.example"]})[0] == 401
    assert api("PUT", "/api/hosts", {"hosts": ["evil.example"]}, token="wrong")[0] == 401
    assert api("GET", "/api/hosts")[0] == 401
    code, out, _ = api("GET", "/api/hosts", token="s3cret")
    assert code == 200 and out["hosts"] == ["a.example"]
