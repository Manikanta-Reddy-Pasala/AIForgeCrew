"""TLS posture of the Jira / Confluence / GitLab HTTP integrations.

The default is INSECURE by the operator's explicit decision: an unset
`{PREFIX}_INSECURE_TLS` skips verification, because these integrations point
at internal, often self-signed, endpoints. These tests pin that default so it
cannot drift silently in either direction, and pin the CA-bundle path — which
keeps verification ON — as the better answer for the self-signed case.
"""
from __future__ import annotations

import ssl

import pytest

from aiforge_core.runtime.tools import _http_integration as hi


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("JIRA_INSECURE_TLS", "JIRA_CA_BUNDLE", "AIFORGE_CA_BUNDLE",
                "JIRA_BASE_URL", "JIRA_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("aiforge_core.config.integrations.get", lambda _n: {})


def _conf(**_kw):
    return hi.integration_conf("jira", "JIRA")


def test_unset_skips_verification_by_design():
    # The operator's deliberate default (see the module docstring). Pinned so
    # the exposure it carries is a decision on record, not an accident: the
    # bearer token travels over an unauthenticated connection.
    conf = _conf()
    assert conf["insecure_tls"] is True
    assert hi.ssl_context(conf["insecure_tls"], conf["ca_bundle"]).verify_mode \
        == ssl.CERT_NONE


def test_explicit_zero_turns_verification_on(monkeypatch):
    monkeypatch.setenv("JIRA_INSECURE_TLS", "0")
    conf = _conf()
    assert conf["insecure_tls"] is False
    # None = urllib's own default verification.
    assert hi.ssl_context(conf["insecure_tls"], conf["ca_bundle"]) is None


def test_explicit_opt_out_still_works(monkeypatch):
    monkeypatch.setenv("JIRA_INSECURE_TLS", "1")
    conf = _conf()
    assert conf["insecure_tls"] is True
    ctx = hi.ssl_context(conf["insecure_tls"], conf["ca_bundle"])
    assert ctx.verify_mode == ssl.CERT_NONE
    assert ctx.check_hostname is False


@pytest.mark.parametrize("value", ["0", "false", "no", ""])
def test_falsey_values_turn_verification_on(monkeypatch, value):
    monkeypatch.setenv("JIRA_INSECURE_TLS", value)
    assert _conf()["insecure_tls"] is False


def test_ca_bundle_keeps_verification_on(monkeypatch, tmp_path):
    import ssl as _ssl
    bundle = tmp_path / "internal-ca.pem"
    bundle.write_text(_read_a_real_ca())
    monkeypatch.setenv("JIRA_CA_BUNDLE", str(bundle))
    conf = _conf()
    ctx = hi.ssl_context(conf["insecure_tls"], conf["ca_bundle"])
    # This is the point of the change: a self-signed internal endpoint is
    # served by trusting its CA, NOT by switching verification off.
    assert ctx.verify_mode == _ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_ca_bundle_wins_over_the_insecure_flag(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text(_read_a_real_ca())
    monkeypatch.setenv("JIRA_CA_BUNDLE", str(bundle))
    monkeypatch.setenv("JIRA_INSECURE_TLS", "1")
    conf = _conf()
    ctx = hi.ssl_context(conf["insecure_tls"], conf["ca_bundle"])
    assert ctx.verify_mode == ssl.CERT_REQUIRED


def test_global_ca_bundle_applies(monkeypatch, tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_text(_read_a_real_ca())
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", str(bundle))
    assert _conf()["ca_bundle"] == str(bundle)


def test_an_unloadable_ca_bundle_raises_rather_than_downgrading(tmp_path):
    missing = str(tmp_path / "nope.pem")
    # Falling back to "no verification" on a typo'd path would be the worst
    # possible failure mode: silently insecure.
    with pytest.raises(ValueError) as err:
        hi.ssl_context(False, missing)
    assert "CA bundle" in str(err.value)


def _read_a_real_ca() -> str:
    """A syntactically valid PEM so create_default_context accepts the file."""
    import certifi
    with open(certifi.where()) as fh:
        text = fh.read()
    end = text.index("-----END CERTIFICATE-----") + len("-----END CERTIFICATE-----")
    return text[:end] + "\n"


# ── plain http:// is reported as unverified transport ───────────────────────

def test_http_fetch_is_reported_as_unverified(monkeypatch):
    """An http:// page is not blocked — much of the web is still http and no
    credential of ours goes with the request — but the model must be told the
    body arrived over a channel nobody authenticated."""
    from aiforge_core.runtime.doer_tools import _web

    class _Resp:
        status = 200
        headers = {"Content-Type": "text/html"}

        def read(self, _n):
            return b"<html>hi</html>"

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(_web, "_open_web_response",
                        lambda req, url: (_Resp(), False))
    monkeypatch.setattr(_web, "_reguard_redirect",
                        lambda *_a, **_kw: None)
    monkeypatch.setattr("aiforge_core.net.ssl.guard_public_url",
                        lambda _u: None)
    out = _web._do_fetch("http://example.com/doc")
    assert out["ok"] is True
    assert out["tls_verified"] is False


def test_https_fetch_carries_no_downgrade_flag(monkeypatch):
    from aiforge_core.runtime.doer_tools import _web

    class _Resp:
        status = 200
        headers = {"Content-Type": "text/html"}

        def read(self, _n):
            return b"ok"

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

    monkeypatch.setattr(_web, "_open_web_response",
                        lambda req, url: (_Resp(), False))
    monkeypatch.setattr(_web, "_reguard_redirect", lambda *_a, **_kw: None)
    monkeypatch.setattr("aiforge_core.net.ssl.guard_public_url", lambda _u: None)
    # Stated only when the answer is "no": a reassurance on every verified
    # fetch is noise the model would repeat forever.
    assert "tls_verified" not in _web._do_fetch("https://example.com/doc")
