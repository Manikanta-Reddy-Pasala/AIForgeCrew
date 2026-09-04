"""A refusal must not be walkable around by changing transport.

Reported from a live session: the user asked for youtube.com, ``web_fetch``
refused it (the host is not on the operator's allowlist), and the agent reran
the same request as a shell ``curl`` — and when that was refused, as a notebook
cell using an HTTP library. The last one worked. Every layer had answered
honestly about its own transport and the request still went out, which makes
the "boundary" the operator was shown a suggestion.

Three layers are pinned here, in the order an agent meets them: the shell
command, the cell that shells out, and the cell that opens a socket itself.
"""
from __future__ import annotations

import socket

import pytest

from aiforge_core.net import egress
from aiforge_core.runtime.tools import kernel_egress, tool_policy

_OFF_LIST = "https://youtube.com/watch?v=1"


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")   # fetching ON…
    for var in ("AIFORGE_WEB_FETCH_DISABLE", "AIFORGE_EGRESS_OFF",
                "AIFORGE_EGRESS_ALLOW_HOSTS", "AIFORGE_KERNEL_EGRESS",
                "AIFORGE_TOOL_POLICY"):
        monkeypatch.delenv(var, raising=False)
    from aiforge_core.config import egress_hosts as _eh
    _eh._invalidate()
    yield
    _eh._invalidate()


# ── layer 1: the shell command ──────────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "curl https://youtube.com",
    "curl -sS youtube.com/watch?v=1",
    "wget https://youtube.com/x.mp4",
    "nc youtube.com 443",
    "cd /tmp && curl -o out.html https://youtube.com",
])
def test_a_fetching_command_answers_to_the_allowlist(cmd):
    refusal = egress.command_refusal(cmd)
    assert refusal is not None, cmd
    assert refusal["error"].startswith("host_not_allowed")


@pytest.mark.parametrize("cmd", [
    "ls -la",
    "echo curling through the data",
    "git commit -m 'curl the docs'",
    "python -m pytest tests/",
])
def test_an_ordinary_command_is_not_touched(cmd):
    assert egress.command_refusal(cmd) is None


@pytest.mark.parametrize("cmd", [
    "curl http://127.0.0.1:5173/health",
    "curl http://localhost:8000/api/ready",
])
def test_the_local_dev_server_stays_reachable(cmd):
    """Loopback is not egress. A lockdown that costs an operator their own app
    is a control doing something nobody asked for."""
    assert egress.command_refusal(cmd) is None


def test_an_allowlisted_host_goes_through(monkeypatch):
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_HOSTS", "docs.python.org")
    from aiforge_core.config import egress_hosts as _eh
    _eh._invalidate()
    assert egress.command_refusal("curl https://docs.python.org/3/") is None


def test_a_search_engine_is_refused_through_curl_too():
    """Web search was removed as a capability; reaching one with curl is the
    capability back, with more steps."""
    refusal = egress.command_refusal(
        "curl 'https://duckduckgo.com/html/?q=my+logs'")
    assert refusal is not None
    assert "web_search_removed" in refusal["error"]


def test_the_refusal_says_the_reroute_is_the_same_request():
    """The message is the fix's other half: an agent that reads "not allowed"
    tries another transport, and one that reads "the same request, refused too"
    reports back instead."""
    refusal = egress.command_refusal("curl https://youtube.com")
    assert "SAME request" in refusal["hint"]


# ── layer 2: the policy gate sees both transports ───────────────────────────

def test_the_gate_denies_the_shell_reroute():
    v = tool_policy.decide("run_command", {"cmd": "curl https://youtube.com"})
    assert v["policy"] == tool_policy.DENY
    assert "host_not_allowed" in v["reason"]


def test_the_gate_denies_a_cell_that_shells_out():
    v = tool_policy.decide("execute_ipython_cell",
                           {"code": "import os\nos.system('curl https://youtube.com')"})
    assert v["policy"] == tool_policy.DENY


def test_the_gate_denies_the_bang_magic_too():
    v = tool_policy.decide("execute_ipython_cell",
                           {"code": "!wget https://youtube.com/x"})
    assert v["policy"] == tool_policy.DENY


def test_deny_is_not_an_approval_question():
    """What is missing is an allowlist entry, not a human's blessing of this
    one call — so an attended chat gets the same answer as a cron run."""
    v = tool_policy.decide("bash", {"cmd": "curl https://youtube.com"})
    assert v["policy"] == tool_policy.DENY
    assert v["policy"] != tool_policy.ASK


def test_an_ordinary_command_still_runs():
    assert tool_policy.decide("run_command", {"cmd": "ls -la"})["policy"] == \
        tool_policy.ALLOW


# ── layer 3: the cell that opens a socket itself ────────────────────────────

_REAL = (socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex)


def _install_guard():
    exec(kernel_egress.guard_source(), {})          # noqa: S102 — that is the test


def _uninstall_guard():
    """Put the real socket functions back.

    By restoring the SAVED originals rather than reloading the module: a
    reload rebuilds socket.socket, and every library in this process holding a
    reference to the old class would quietly be using a different one for the
    rest of the run — a fine way to make an unrelated test fail an hour later.
    """
    socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = _REAL
    if hasattr(socket, "_aiforge_egress_guarded"):
        delattr(socket, "_aiforge_egress_guarded")


@pytest.fixture
def guarded():
    _install_guard()
    yield
    _uninstall_guard()


def test_a_library_call_in_the_kernel_is_refused(guarded):
    """This is the transport that actually worked in the report. requests,
    urllib and httpx all end up in getaddrinfo."""
    with pytest.raises(OSError) as exc:
        socket.getaddrinfo("youtube.com", 443)
    assert "egress policy" in str(exc.value)


def test_loopback_still_resolves_in_the_kernel(guarded):
    assert socket.getaddrinfo("127.0.0.1", 80)


def test_an_allowlisted_host_resolves_in_the_kernel(monkeypatch):
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_HOSTS", "example.com")
    from aiforge_core.config import egress_hosts as _eh
    _eh._invalidate()
    _install_guard()
    try:
        # No network call is made by the policy itself; a DNS failure here is a
        # network fact, not a refusal, so accept either outcome except OUR error.
        try:
            socket.getaddrinfo("example.com", 443)
        except OSError as exc:
            assert "egress policy" not in str(exc)
    finally:
        _uninstall_guard()


def test_a_hardcoded_public_ip_is_refused(guarded):
    """Pre-resolving is the obvious way around a name check, so the address
    path refuses any public IP that did not come out of a resolution the policy
    allowed."""
    s = socket.socket()
    try:
        with pytest.raises(OSError) as exc:
            s.connect(("142.250.72.14", 443))
        assert "egress policy" in str(exc.value)
    finally:
        s.close()


def test_the_guard_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AIFORGE_KERNEL_EGRESS", "0")
    assert kernel_egress.guard_source() == ""
    assert kernel_egress.enabled() is False


def test_the_guard_is_installed_before_any_cell_runs(monkeypatch):
    """Pin the WIRING: a guard that is never exec'd in the kernel has stopped
    nothing, and this one lives in a string that nothing else would notice."""
    from aiforge_core.runtime.tools import ipython_kernel as ik

    sent: list[str] = []

    class _Client:
        def execute(self, code, **_kw):
            sent.append(code)
            return "msg-id"

        def start_channels(self):
            pass

        def wait_for_ready(self, timeout=10):
            pass

    class _KM:
        def start_kernel(self):
            pass

        def client(self):
            return _Client()

    monkeypatch.setattr(ik, "_drain_iopub", lambda *a, **k: {})
    monkeypatch.setitem(__import__("sys").modules, "jupyter_client.manager",
                        type("M", (), {"KernelManager": _KM}))
    ik._kernels.pop("wiring-test", None)
    ik._clients.pop("wiring-test", None)
    try:
        ik._start_kernel("wiring-test")
    finally:
        ik._kernels.pop("wiring-test", None)
        ik._clients.pop("wiring-test", None)
    assert sent, "nothing was executed in the kernel at all"
    assert "_aiforge_install_egress_guard" in sent[0], \
        "the egress guard must run BEFORE the AgentSkills bootstrap"


# ── the shapes a first cut missed ───────────────────────────────────────────

@pytest.mark.parametrize("cmd", [
    "/usr/bin/curl https://youtube.com",
    "./curl youtube.com",
    "cat x | xargs curl https://youtube.com",
    'python -c "import requests; requests.get(\'https://youtube.com\')"',
    'python3 -c "import urllib.request as u; u.urlopen(\'http://youtube.com\')"',
    'node -e "fetch(\'https://youtube.com\')"',
])
def test_the_obvious_next_thing_to_try_is_covered(cmd):
    """Each of these was walked past the first version of the check: an
    absolute path instead of the bare word, and an interpreter handed the
    request as a program so no fetcher is named at all."""
    assert egress.command_refusal(cmd) is not None, cmd


@pytest.mark.parametrize("cmd", [
    "curl -o out.html http://127.0.0.1:8000/x",
    "curl --output report.json http://localhost:9000/api",
    "python -m pytest tests/",
    "python script.py",
    'python -c "print(1)"',
    "git clone https://github.com/example/repo",
])
def test_no_false_refusals(cmd):
    """An output FILENAME is not a destination — reading `out.html` as a host
    would refuse a fetch the operator explicitly allowed. And git is left out
    on purpose: a push already gates as caution, and treating every remote as
    egress would break ordinary work for no gain here."""
    assert egress.command_refusal(cmd) is None, cmd


def test_a_lan_destination_is_not_egress():
    assert egress.command_refusal("nc 10.0.0.5 22") is None
