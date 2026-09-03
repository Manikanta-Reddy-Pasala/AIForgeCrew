"""The allowlist itself: where its entries come from, and that it denies.

The operator's rule is "egress enforcement is always on, only the configured
integrations are reachable, and Settings can add a few more". The awkward part
is the first clause — a default-deny list that has to be typed by hand is a
list nobody maintains, so the base entries are DERIVED from configuration that
already exists.
"""
from __future__ import annotations

import pytest

from aiforge_core.config import egress_hosts


@pytest.fixture(autouse=True)
def _no_stale_derivation():
    """The derived host set is memoized for a few seconds (it touches ~20 files
    and runs on every outbound decision). A test that sets an env var and asks
    immediately would otherwise read the previous answer."""
    from aiforge_core.config import egress_hosts as _eh
    _eh._invalidate()
    yield
    _eh._invalidate()


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for var in ("AIFORGE_EGRESS_ALLOW_HOSTS", "LANGFUSE_HOST",
                "AIFORGE_LM_BASE_URL", "AIFORGE_EMBED_BASE_URL",
                "AIFORGE_RERANK_BASE_URL", "AIFORGE_ADMIN_URL",
                "AIFORGE_MCP_ENDPOINTS"):
        monkeypatch.delenv(var, raising=False)


# ── derived: configuration you already wrote is not typed twice ─────────────

def test_the_model_endpoint_is_allowed_without_being_listed(monkeypatch):
    """If the agent could not reach its own model it could not run at all —
    the list must never be the thing that breaks that."""
    monkeypatch.setenv("AIFORGE_LM_BASE_URL", "http://192.168.70.185:8081/v1")
    assert "192.168.70.185" in egress_hosts.allowed_hosts()


def test_observability_and_mcp_endpoints_are_derived(monkeypatch):
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.corp.example")
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS",
                       "one=https://mcp1.corp.example/rpc,two=https://mcp2.corp.example/rpc")
    hosts = egress_hosts.allowed_hosts()
    assert {"langfuse.corp.example", "mcp1.corp.example",
            "mcp2.corp.example"} <= hosts


def test_a_broken_probe_does_not_empty_the_whole_list(monkeypatch):
    """Each source is guarded separately: one half-configured integration must
    not fail the box closed on everything at once, the model included."""
    monkeypatch.setenv("AIFORGE_LM_BASE_URL", "http://model.corp.example/v1")

    def _boom():
        raise RuntimeError("jira config is broken")

    monkeypatch.setattr("aiforge_core.runtime.tools.jira._core._base", _boom)
    assert "model.corp.example" in egress_hosts.allowed_hosts()


# ── stored: the few an operator adds in Settings ────────────────────────────

def test_settings_extras_round_trip():
    saved = egress_hosts.set_stored_hosts(
        ["https://docs.python.org/3/", "pypi.org", "docs.python.org"])
    assert saved == ["docs.python.org", "pypi.org"], "not normalised/deduped"
    assert set(saved) <= egress_hosts.allowed_hosts()


def test_a_url_or_a_bare_host_are_both_accepted():
    """An operator pastes whatever they have — a full URL, a host, a host:port."""
    saved = egress_hosts.set_stored_hosts(
        ["https://a.example/path?q=1", "b.example:8443", "  c.example  "])
    assert saved == ["a.example", "b.example", "c.example"]


def test_a_corrupt_store_does_not_open_the_gate(tmp_path):
    (tmp_path / "egress.json").write_text("{ not json")
    assert egress_hosts.stored_hosts() == []


def test_removing_a_host_from_settings_removes_the_permission():
    egress_hosts.set_stored_hosts(["temporary.example"])
    assert "temporary.example" in egress_hosts.allowed_hosts()
    egress_hosts.set_stored_hosts([])
    assert "temporary.example" not in egress_hosts.allowed_hosts()


def test_describe_says_where_each_entry_came_from(monkeypatch):
    """The Settings screen has to show that the Jira host is already covered,
    or an operator will add it by hand and then wonder why removing it does
    nothing."""
    monkeypatch.setenv("AIFORGE_LM_BASE_URL", "http://model.corp.example/v1")
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_HOSTS", "from-env.example")
    egress_hosts.set_stored_hosts(["from-settings.example"])
    d = egress_hosts.describe()
    assert "model.corp.example" in d["derived"]
    assert d["extra_hosts"] == ["from-settings.example"]
    assert "from-env.example" in d["env"]
    assert set(d["effective"]) >= {"model.corp.example", "from-settings.example",
                                   "from-env.example"}


# ── reading is not writing ──────────────────────────────────────────────────
# Adding a docs site to the allowlist must not also create somewhere to post
# our data. Reading pulls bytes in; writing pushes ours out, and only the
# second is exfiltration.

def test_a_settings_host_is_readable_but_not_writable(monkeypatch):
    monkeypatch.setenv("AIFORGE_ALLOW_WEB_FETCH", "1")
    monkeypatch.setenv("AIFORGE_UNATTENDED_WRITES", "1")  # isolate this rule
    from aiforge_core.net import egress

    egress_hosts.set_stored_hosts(["docs.python.org"])
    assert egress.check("https://docs.python.org/3/") is None
    refusal = egress.allow("integration", "https://docs.python.org/submit",
                           method="POST")
    assert (refusal or {}).get("error") == "host_not_writable"


def test_a_configured_integration_IS_writable(monkeypatch):
    """The distinction is not "trusted host" — it is "was this set up as a
    destination, with a base URL and a credential, on purpose"."""
    monkeypatch.setenv("AIFORGE_UNATTENDED_WRITES", "1")
    monkeypatch.setenv("JIRA_BASE_URL", "https://jira.corp.example")
    monkeypatch.setenv("JIRA_TOKEN", "t")
    from aiforge_core.net import egress

    assert "jira.corp.example" in egress_hosts.write_hosts()
    assert egress.allow("integration", "https://jira.corp.example/rest/x",
                        method="POST") is None


def test_an_upload_to_a_settings_host_is_refused(monkeypatch):
    """An upload is the loudest form of the same thing — file content, not a
    sentence — so it must not slip past on a GET."""
    monkeypatch.setenv("AIFORGE_UNATTENDED_WRITES", "1")
    from aiforge_core.net import egress

    egress_hosts.set_stored_hosts(["files.example"])
    assert (egress.allow("integration", "https://files.example/up",
                         method="GET", upload=True) or {}
            ).get("error") == "host_not_writable"


def test_the_write_list_excludes_env_and_settings_entries(monkeypatch):
    monkeypatch.setenv("AIFORGE_EGRESS_ALLOW_HOSTS", "from-env.example")
    egress_hosts.set_stored_hosts(["from-settings.example"])
    writable = egress_hosts.write_hosts()
    assert "from-env.example" not in writable
    assert "from-settings.example" not in writable


def test_describe_names_the_writable_subset():
    """The screen has to say it, or an operator adds a host expecting the agent
    to be able to post to it."""
    egress_hosts.set_stored_hosts(["read-only.example"])
    d = egress_hosts.describe()
    assert "read-only.example" in d["effective"]
    assert "read-only.example" not in d["writable"]


# ── the invariant: every consumer's hosts are on the list ───────────────────
# This is the test that would have caught the original defect class in one go.
# The first derivation was a hand-kept list of env names, so it missed the MCP
# marketplace registry, the model registry, a Settings-saved sync admin and
# several sidecars — each miss a feature that dies silently the moment the list
# defaults to deny. Asserting against the CONSUMERS, rather than re-listing the
# env vars here, means a new endpoint source fails this test instead of failing
# in production.

def test_every_mcp_endpoint_the_client_would_use_is_allowed(monkeypatch):
    monkeypatch.setenv("AIFORGE_MCP_ENDPOINTS",
                       "a=https://mcp-a.example/rpc,b=https://mcp-b.example/rpc")
    from aiforge_core.runtime.tools.mcp_client import _load_endpoints

    egress_hosts._invalidate()
    allowed = egress_hosts.allowed_hosts()
    for name, url in (_load_endpoints() or {}).items():
        host = egress_hosts._host_of(url)
        assert host in allowed, f"MCP endpoint {name} ({host}) would be refused"


def test_every_model_endpoint_the_registry_holds_is_allowed():
    from aiforge_core.config import model_registry

    egress_hosts._invalidate()
    allowed = egress_hosts.allowed_hosts()
    for row in (model_registry.list_models() or []):
        host = egress_hosts._host_of(str(row.get("base_url") or ""))
        if host:
            assert host in allowed, f"registry model host {host} would be refused"


def test_the_sync_admin_is_allowed_even_when_set_from_settings(monkeypatch):
    """role.admin_url() reads Settings as well as the env — deriving only the
    env form left a Settings-saved admin unlisted, and the sync daemon would
    have posted to a host the policy refused."""
    from aiforge_core.memory.sync import role

    monkeypatch.setattr(role, "admin_url", lambda: "https://hub.example:8799")
    egress_hosts._invalidate()
    assert "hub.example" in egress_hosts.allowed_hosts()


def test_the_ssl_service_inventory_is_a_subset(monkeypatch):
    """net/ssl.py already maintained the correct endpoint inventory. Deriving a
    SECOND, shorter one beside it is what produced the original gaps, so pin
    that the egress list never falls behind it."""
    monkeypatch.setenv("AIFORGE_EMBED_URL", "http://embed.example:9000")
    monkeypatch.setenv("AIFORGE_SOMETHING_BASE_URL", "http://svc.example:81")
    from aiforge_core.net.ssl import _configured_service_hosts

    egress_hosts._invalidate()
    allowed = egress_hosts.allowed_hosts()
    missing = _configured_service_hosts() - allowed
    assert not missing, f"configured service hosts not on the egress list: {missing}"
