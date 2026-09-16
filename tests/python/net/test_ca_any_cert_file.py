"""A corporate root arrives under whatever name the PKI gave it.

Only `custom-ca.pem` was ever read, so an operator who dropped the `.crt` their
internal CA page served, or the `.cer` Windows exported, got silence — and
every https call kept failing with a certificate error while the file sat right
there in the folder.
"""
from __future__ import annotations

import ssl
import subprocess
import tempfile

import pytest

from aiforge_core.net import ca


@pytest.fixture
def cadir(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for var in ca.ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    d = ca.ca_dir(create=True)
    return d


def _self_signed_pem() -> str:
    """A real certificate, so the DER path is exercised for real."""
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", f"{td}/k.pem", "-out", f"{td}/c.pem", "-days", "1",
             "-subj", "/CN=AIForge Test Root"],
            check=True, capture_output=True)
        return open(f"{td}/c.pem", encoding="utf-8").read()


@pytest.mark.parametrize("name", ["custom-ca.pem", "corp-root.crt",
                                  "internal.cert", "whatever.pem"])
def test_a_dropped_pem_is_found_under_any_name(cadir, name):
    (cadir / name).write_text(_self_signed_pem(), encoding="utf-8")
    assert ca.bundle(), f"{name} was ignored"
    assert "-----BEGIN CERTIFICATE-----" in open(ca.bundle(), encoding="utf-8").read()


def test_a_der_cer_is_converted_not_concatenated_raw(cadir):
    """A .cer from Windows is DER — binary. Pasted into a bundle as-is it reads
    as empty and the operator sees no change at all."""
    der = ssl.PEM_cert_to_DER_cert(_self_signed_pem())
    (cadir / "windows-export.cer").write_bytes(der)
    out = ca.bundle()
    assert out, "a DER .cer was ignored"
    text = open(out, encoding="utf-8").read()
    assert text.count("-----BEGIN CERTIFICATE-----") == 1


def test_a_root_and_an_intermediate_are_both_in_the_bundle(cadir):
    """An estate issues a chain; trusting only the first file we glob leaves
    internal hosts failing for a reason nothing reports."""
    (cadir / "root.crt").write_text(_self_signed_pem(), encoding="utf-8")
    (cadir / "intermediate.crt").write_text(_self_signed_pem(), encoding="utf-8")
    text = open(ca.bundle(), encoding="utf-8").read()
    assert text.count("-----BEGIN CERTIFICATE-----") == 2


def test_an_unreadable_file_is_named_and_does_not_poison_the_bundle(cadir, caplog):
    (cadir / "good.crt").write_text(_self_signed_pem(), encoding="utf-8")
    (cadir / "notes.cer").write_bytes(b"this is not a certificate at all")
    with caplog.at_level("ERROR"):
        text = open(ca.bundle(), encoding="utf-8").read()
    assert text.count("-----BEGIN CERTIFICATE-----") == 1
    assert "notes.cer" in caplog.text        # the operator is told WHICH file


def test_a_private_key_is_never_read_into_the_trust_bundle(cadir):
    (cadir / "server.key").write_text("-----BEGIN PRIVATE KEY-----\nx\n"
                                      "-----END PRIVATE KEY-----\n", encoding="utf-8")
    assert ca.bundle() is None
    assert not [p for p in ca.dropped_certs() if p.suffix == ".key"]


def test_an_env_var_still_wins(cadir, monkeypatch):
    (cadir / "corp-root.crt").write_text(_self_signed_pem(), encoding="utf-8")
    monkeypatch.setenv("AIFORGE_CA_BUNDLE", "/etc/ssl/operator.pem")
    assert ca.bundle() == "/etc/ssl/operator.pem"
    assert ca.source() == "AIFORGE_CA_BUNDLE"


def test_source_reports_a_dropped_file_as_dropped(cadir):
    (cadir / "corp-root.crt").write_text(_self_signed_pem(), encoding="utf-8")
    assert ca.source() == "dropped"


def test_nothing_dropped_is_still_nothing(cadir):
    assert ca.bundle() is None
    assert ca.source() == ""
