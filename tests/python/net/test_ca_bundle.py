"""One CA bundle, honoured by the model client, the integrations AND git.

Before ``net.ca`` each subsystem read its own variable and subprocesses read
none of them, so an internal CA reached the model endpoint and the Jira REST
calls while ``git clone`` against the same estate still failed with a
certificate error. These tests pin the two properties that fixed it: one
variable resolves everywhere, and it beats trust-on-first-use.
"""
from __future__ import annotations

import pytest

from aiforge_core.net import ca


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in set(ca.ENV_VARS) | set(ca.SUBPROCESS_VARS) | {
            "AIFORGE_LLM_CA_BUNDLE"}:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def pem(tmp_path):
    p = tmp_path / "internal-ca.pem"
    p.write_text("-----BEGIN CERTIFICATE-----\nnot-a-real-cert\n"
                 "-----END CERTIFICATE-----\n")
    return str(p)


# ── resolution ──────────────────────────────────────────────────────────────

def test_no_bundle_configured_is_none():
    assert ca.bundle() is None


@pytest.mark.parametrize("var", ca.ENV_VARS)
def test_every_documented_variable_resolves(monkeypatch, pem, var):
    monkeypatch.setenv(var, pem)
    assert ca.bundle() == pem


def test_aiforge_var_wins_over_the_standard_ones(monkeypatch, pem, tmp_path):
    other = tmp_path / "other.pem"
    other.write_text("x")
    monkeypatch.setenv("SSL_CERT_FILE", str(other))
    monkeypatch.setenv("REQUESTS_CA_BUNDLE", str(other))
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", pem)
    assert ca.bundle() == pem


def test_whitespace_only_is_not_a_bundle(monkeypatch):
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", "   ")
    assert ca.bundle() is None


def test_readable_is_false_for_a_missing_file(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", str(tmp_path / "absent.pem"))
    assert ca.bundle()          # configured …
    assert ca.readable() is False   # … but not usable, and we say so


# ── subprocesses ────────────────────────────────────────────────────────────

def test_subprocess_env_fills_every_tool_variable(monkeypatch, pem):
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", pem)
    env = ca.subprocess_env({})
    for var in ca.SUBPROCESS_VARS:
        assert env[var] == pem, f"{var} would not see the CA"


def test_subprocess_env_never_overrules_an_operator(monkeypatch, pem):
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", pem)
    env = ca.subprocess_env({"GIT_SSL_CAINFO": "/etc/ssl/git-only.pem"})
    assert env["GIT_SSL_CAINFO"] == "/etc/ssl/git-only.pem"
    assert env["CURL_CA_BUNDLE"] == pem


def test_subprocess_env_adds_nothing_when_unconfigured():
    assert not (set(ca.subprocess_env({})) & set(ca.SUBPROCESS_VARS))


def test_apply_to_process_env_publishes_it(monkeypatch, pem):
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", pem)
    import os
    assert ca.apply_to_process_env() == pem
    assert os.environ["GIT_SSL_CAINFO"] == pem   # git clone now verifies


def test_apply_is_a_no_op_without_a_bundle():
    import os
    assert ca.apply_to_process_env() is None
    assert "GIT_SSL_CAINFO" not in os.environ


def test_an_unreadable_bundle_is_still_exported_and_logged(
        monkeypatch, tmp_path, caplog):
    """Loud beats silent: git says 'cannot access the CA file' in one line,
    where dropping it would leave the operator believing theirs was in use."""
    missing = str(tmp_path / "absent.pem")
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", missing)
    with caplog.at_level("ERROR"):
        assert ca.apply_to_process_env() == missing
    import os
    assert os.environ["GIT_SSL_CAINFO"] == missing
    assert any("not readable" in r.getMessage() for r in caplog.records)


# ── the callers ─────────────────────────────────────────────────────────────

def test_model_client_uses_the_shared_bundle(monkeypatch, pem):
    from aiforge_core.net import ssl as nssl
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", pem)
    assert nssl._ca_bundle() == pem


def test_llm_specific_variable_still_overrides(monkeypatch, pem, tmp_path):
    from aiforge_core.net import ssl as nssl
    llm = tmp_path / "model-ca.pem"
    llm.write_text("x")
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", pem)
    monkeypatch.setenv("AIFORGE_LLM_CA_BUNDLE", str(llm))
    assert nssl._ca_bundle() == str(llm)


def test_integrations_pick_up_the_shared_bundle(monkeypatch, pem):
    from aiforge_core.runtime.tools import _http_integration as hi
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", pem)
    conf = hi.integration_conf("jira", "JIRA")
    assert conf["ca_bundle"] == pem
    # …and the CA answers the self-signed case, so TOFU is not the default.
    assert conf["insecure_tls"] is False


def test_integrations_still_default_to_pinning_without_a_bundle(monkeypatch):
    from aiforge_core.runtime.tools import _http_integration as hi
    monkeypatch.delenv("JIRA_INSECURE_TLS", raising=False)
    conf = hi.integration_conf("jira", "JIRA")
    assert conf["ca_bundle"] == ""
    assert conf["insecure_tls"] is True


def test_a_ca_bundle_beats_trust_on_first_use(monkeypatch, pem):
    """With a CA to verify against, nothing should pin a host's own cert."""
    from aiforge_core.runtime.tools import _http_integration as hi
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", pem)
    called = []
    from aiforge_core.net import ssl as nssl
    monkeypatch.setattr(nssl, "insecure_context",
                        lambda *a, **k: called.append(a) or None)
    with pytest.raises(ValueError):   # our pem is not a real certificate
        hi.ssl_context(insecure_tls=True, ca_bundle=pem,
                       url="https://jira.internal")
    assert not called, "TOFU ran even though a CA bundle was configured"
