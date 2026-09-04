"""Every outbound page path answers to the SAME two switches.

Written after a review found that ``AIFORGE_WEB_FETCH_DISABLE`` — documented in
.env.example as the hard-off for a box that must not talk to the open web —
was consulted by exactly two of the six paths, and that ``web_crawl`` skipped
``AIFORGE_ALLOW_WEB_FETCH`` entirely for every role because the doer wrapper
hardcoded ``sanctioned: True``. Both switches read correct; the door was open.

Each case here fails on the pre-fix tree. Keep them per-path rather than
testing ``egress.check`` alone: the defect was never in the decision, it was in
which callers bothered to ask.
"""
from __future__ import annotations

import pytest

from aiforge_core.net import egress
from aiforge_core.runtime import doer_tools
from aiforge_core.runtime.tools import browser as browser_tool
from aiforge_core.runtime.tools import web_fetch as wf
from aiforge_core.runtime.tools import web_ingest

_URL = "https://example.com/docs"
_SEARCH = "https://html.duckduckgo.com/html/?q=a+line+from+my+logs"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("AIFORGE_ALLOW_WEB_FETCH", "AIFORGE_WEB_FETCH_DISABLE",
                "AIFORGE_WEB_SEARCH_DISABLE", "AIFORGE_BROWSER_ALLOWLIST",
                "AIFORGE_EGRESS_OFF"):
        monkeypatch.delenv(var, raising=False)


# A REFUSAL, not merely a failure. `ok: False` alone is satisfied by a DNS
# error or an offline runner, so an early version of these tests passed against
# the ungated tree — the assertion was true for the wrong reason. Pin the error
# the gate itself produces, and require that nothing reached the network.
_OFF = "web fetch disabled (set AIFORGE_ALLOW_WEB_FETCH=1)"


def _refused(result, expected: str) -> bool:
    return (isinstance(result, dict) and result.get("ok") is False
            and result.get("error") == expected)


# ── the switch is off (the default) ─────────────────────────────────────────

@pytest.mark.parametrize("call", [
    lambda: wf.web_fetch({"url": _URL}),
    lambda: doer_tools.fetch_url(_URL),
    lambda: doer_tools.http_get(_URL),
    lambda: doer_tools.web_read(_URL),
    lambda: doer_tools.web_crawl(_URL),
    lambda: web_ingest.web_crawl({"url": _URL}),
])
def test_every_path_refuses_while_fetching_is_off(call):
    out = call()
    assert _refused(out, _OFF), (
        f"path did not refuse with the switch off: {out!r}")


def test_sanctioned_flag_no_longer_bypasses_the_switch():
    """It was the bypass that made the switch a lie — the doer wrapper passed
    it unconditionally, so 'researcher-only sanctioned egress' meant every
    role."""
    assert _refused(web_ingest.web_crawl({"url": _URL, "sanctioned": True}), _OFF)


# ── the hard-off wins over the switch ───────────────────────────────────────

@pytest.mark.parametrize("kill_var", ["AIFORGE_WEB_FETCH_DISABLE",
                                      "AIFORGE_WEB_SEARCH_DISABLE",
                                      # The master switch reads as "nothing
                                      # leaves this box". It closed the four
                                      # DECLARED classes and left the widest
                                      # channel — a model-composed URL — wide
                                      # open, while run.sh turns fetching on.
                                      "AIFORGE_EGRESS_OFF"])
@pytest.mark.parametrize("call", [
    lambda: wf.web_fetch({"url": _URL}),
    lambda: doer_tools.fetch_url(_URL),
    lambda: doer_tools.web_read(_URL),
    lambda: doer_tools.web_crawl(_URL),
])
def test_hard_off_beats_the_allow_flag(monkeypatch, kill_var, call):
    """Including under the LEGACY name: a box locked down before the search
    code was deleted must not reopen because the variable was renamed."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.setenv(kill_var, "1")
    out = call()
    assert _refused(out, "web_fetch_disabled"), (
        f"the hard-off did not close this path: {out!r}")


# ── a search URL is refused however the switches are set ────────────────────

@pytest.mark.parametrize("call", [
    lambda: wf.web_fetch({"url": _SEARCH}),
    lambda: doer_tools.fetch_url(_SEARCH),
    lambda: doer_tools.web_read(_SEARCH),
    lambda: doer_tools.web_crawl(_SEARCH),
])
def test_search_engine_urls_are_refused_even_with_fetching_on(monkeypatch, call):
    """Deleting the tool means nothing if the model can just fetch the URL."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    out = call()
    assert _refused(out, "web_search_removed"), (
        f"a search engine URL was not refused: {out!r}")


@pytest.mark.parametrize("url,expected", [
    ("https://html.duckduckgo.com/html/?q=x", True),
    ("https://www.google.com/search?q=x", True),
    ("https://api.tavily.com/search?q=x", True),
    ("https://duckduckgo.com", False),          # homepage carries no payload
    ("https://evil.example/#google.com", False),  # fragment must not match
    ("https://notgoogle.com/?q=x", False),      # suffix match, not substring
    ("https://docs.python.org/3/library/os.html", False),
])
def test_search_url_detection(url, expected):
    assert egress.looks_like_search(url) is expected


# ── browse: external is egress, localhost is not ────────────────────────────

def test_browse_allowlist_is_the_operator_permission_but_hard_off_wins(monkeypatch):
    """An explicit allowlist entry is a deliberate operator act, so it is not
    second-guessed by the fetch switch. The HARD-off means "this box does not
    talk out" and beats it."""
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", "github.com")
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "0")
    assert browser_tool._allowlist_ok("https://github.com/x") is True
    monkeypatch.setenv("AIFORGE_WEB_FETCH_DISABLE", "1")
    assert browser_tool._allowlist_ok("https://github.com/x") is False


@pytest.mark.parametrize("url", [
    "http://localhost:5173/", "http://127.0.0.1:8080/", "http://[::1]:3000/",
    "http://192.168.1.20:5173/",          # a LAN dev box, by IP
    "http://host.docker.internal:8080/",  # a container host (exact name)
    "http://myapp.localhost:8080/",       # a Traefik-style vhost
])
def test_local_dev_servers_stay_browsable_under_the_lockdown(monkeypatch, url):
    """NOTE: a LAN box reached by NAME (dev.lan, foo.internal) is no longer
    "local" — see test_a_name_suffix_cannot_prove_locality. Reach it by IP, or
    allowlist it."""
    """The lockdown is about EGRESS. An operator who locks the box down and
    clears the browser allowlist (the natural thing to do) must not lose
    ui_check against their own dev server — that is a control doing something
    nobody asked for. Only loopback was covered at first, so a LAN box, a
    container host and a *.localhost vhost all went dark."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "0")
    monkeypatch.setenv("AIFORGE_WEB_FETCH_DISABLE", "1")
    assert browser_tool._allowlist_ok(url) is True


def test_the_master_switch_closes_browsing_too(monkeypatch):
    """browse is the widest page tool in the system; a switch that stops
    web_fetch and leaves a headless browser driving is not a kill switch.

    The host is put ON both allowlists first — otherwise this passes because
    example.com is simply not on the list, which is true with or without the
    fix and proves nothing."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_HOSTS", "example.com")
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", "example.com")
    from aiforge_core.config import egress_hosts as _eh
    _eh._invalidate()
    assert browser_tool._allowlist_ok(_URL) is True      # control
    monkeypatch.setenv("AIFORGE_EGRESS_OFF", "1")
    assert browser_tool._allowlist_ok(_URL) is False


def test_browse_refuses_a_search_engine(monkeypatch):
    monkeypatch.setenv("AIFORGE_BROWSER_ALLOWLIST", "google.com")
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    assert browser_tool._allowlist_ok("https://www.google.com/search?q=x") is False


# ── the model asking for the removed tool gets the reason ───────────────────

@pytest.mark.parametrize("name", ["web_search", "search_web", "google_search",
                                  "ddg_search", "search-web"])
def test_a_removed_search_call_explains_itself(name):
    """A bare 'unknown tool' sends the model round the alias carousel and it
    often answers from memory instead."""
    from aiforge_core.runtime.chat_agent._loop import _unknown_tool_result

    out = _unknown_tool_result(name)
    assert out["error"] == "web_search_removed"
    assert "ask the user" in out["hint"]


def test_an_ordinary_unknown_tool_is_still_an_ordinary_error():
    from aiforge_core.runtime.chat_agent._loop import _unknown_tool_result

    assert _unknown_tool_result("frobnicate")["error"] == "unknown tool: frobnicate"


# ── redirects are re-guarded, not just the URL we were handed ───────────────

def _redirecting_opener(new_url: str):
    """An opener whose first hop 302s to ``new_url`` — i.e. the real handler
    chain runs, so the guard under test is actually exercised."""

    class _Op:
        def __init__(self, ctx):
            self._h = wf._GuardedRedirect()

        def open(self, req, timeout=None):
            return self._h.redirect_request(
                req, None, 302, "Found", {"location": new_url}, new_url)

    return _Op


@pytest.mark.parametrize("target", [
    "http://169.254.169.254/latest/meta-data/",   # cloud credentials
    "https://html.duckduckgo.com/html/?q=leak",   # search by another route
])
def test_a_redirect_cannot_escape_the_guards(monkeypatch, target):
    """Guarding only the URL we were given checks the one hop an attacker
    controls least: ``https://ok.example/x`` can 302 anywhere, and the body
    used to come back as an ordinary success."""

    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.setattr(wf, "_opener", _redirecting_opener(target))
    # Keep the ENTRY url past the guards so the failure can only come from the
    # redirect handler: on a resolver that wildcards NXDOMAIN to a private
    # address, ok.example itself is refused and this would pass having never
    # reached the code under test.
    monkeypatch.setattr("aiforge_core.net.ssl.guard_public_url",
                        lambda u: None if "ok.example" in u else _raise_ssrf(u))
    out = wf.web_fetch({"url": "https://ok.example/x"})
    assert out["ok"] is False
    assert "refused" in str(out.get("error", "")) or "ssrf" in str(
        out.get("error", "")), out


def test_a_harmless_redirect_still_follows(monkeypatch):
    """The guard must refuse a bad hop, not every hop — and must hand back a
    request aimed at the NEW url."""
    monkeypatch.setattr("aiforge_core.net.ssl.guard_public_url", lambda u: None)
    handler = wf._GuardedRedirect()
    req = handler.redirect_request(
        _FakeReq("https://ok.example/x"), None, 302, "Found",
        {"location": "https://ok.example/y"}, "https://ok.example/y")
    assert req is not None
    assert req.get_full_url() == "https://ok.example/y"


def _raise_ssrf(url):
    from aiforge_core.net.ssl import SSRFBlocked

    raise SSRFBlocked("blocked non-public address", kind="private")


class _FakeReq:
    def __init__(self, url):
        self.full_url = url
        self.headers = {}
        self.unredirected_hdrs = {}
        self.data = None
        self.origin_req_host = "ok.example"
        self.unverifiable = False

    def get_full_url(self):
        return self.full_url

    def get_method(self):
        return "GET"


def test_metadata_service_is_never_local(monkeypatch):
    """A link-local address is not "my network" — 169.254.169.254 hands out
    cloud credentials, and the local-host branch must not launder it."""
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "0")
    assert browser_tool._allowlist_ok("http://169.254.169.254/latest/") is False
    assert browser_tool._is_local_host("169.254.169.254") is False


def test_the_real_opener_carries_the_redirect_guard():
    """Every other redirect test patches `_opener` out, so the WIRING — that
    build_opener actually installs _GuardedRedirect — was pinned by nothing.
    Deleting the handler from the call would have left the suite green."""
    import ssl

    opener = wf._opener(ssl.create_default_context())
    assert any(isinstance(h, wf._GuardedRedirect) for h in opener.handlers), \
        "web_fetch's opener no longer installs the redirect guard"


def test_a_junk_timeout_does_not_break_a_fetch(monkeypatch):
    """A typo in AIFORGE_WEB_TIMEOUT_S must not raise into the agent loop —
    the soft-error contract says these functions never raise."""
    monkeypatch.setenv("AIFORGE_WEB_TIMEOUT_S", "soon")
    assert wf._timeout() == 12.0


def test_the_catalog_gate_fails_open_but_says_so(monkeypatch):
    """Deliberate: a broken probe must not break a turn, so the tools stay
    advertised. Safe only because the tools refuse independently — the prompt
    is a hint, never the boundary. Pinned so a future change to fail closed is
    a conscious one."""
    from aiforge_core.net import egress as _e
    from aiforge_core.runtime.chat_agent import _catalog_gate

    def _boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(_e, "fetch_allowed", _boom)
    assert _catalog_gate._web_fetch_on() is True
