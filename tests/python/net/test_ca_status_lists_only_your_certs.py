"""The Settings panel shows what YOU added, not the whole trust store.

The bundle in force is merged with the platform's roots, because SSL_CERT_FILE
REPLACES the trust store rather than adding to it. Describing that bundle put
~150 public roots on the screen and buried the one file the operator uploaded.
"""
from __future__ import annotations

import subprocess
import tempfile

import pytest

from aiforge_core.net import ca


@pytest.fixture
def cadir(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for var in ca.ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    return ca.ca_dir(create=True)


def _cert(cn: str) -> str:
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
             "-keyout", f"{td}/k.pem", "-out", f"{td}/c.pem", "-days", "1",
             "-subj", f"/CN={cn}"], check=True, capture_output=True)
        return open(f"{td}/c.pem", encoding="utf-8").read()


def test_only_the_operators_certificates_are_listed(cadir, monkeypatch):
    mine = _cert("Corp Root")
    (cadir / "corp-root.crt").write_text(mine, encoding="utf-8")
    # a bundle in force that also carries unrelated roots, as run.sh builds it
    merged = cadir / "bundle-with-system.pem"
    merged.write_text(mine + _cert("Public Root A") + _cert("Public Root B"),
                      encoding="utf-8")
    monkeypatch.setenv("SSL_CERT_FILE", str(merged))

    st = ca.status()
    subjects = [c["subject"] for c in st["certificates"]]
    assert len(st["certificates"]) == 1
    assert any("Corp Root" in s for s in subjects)
    assert not any("Public Root" in s for s in subjects)
    assert st["bundle_total"] == 3
    assert st["others_in_bundle"] == 2       # counted, never hidden


def test_each_certificate_says_where_it_came_from(cadir):
    (cadir / "corp-root.crt").write_text(_cert("Dropped Root"), encoding="utf-8")
    ca.save(_cert("Pasted Root"))
    origins = {c["origin"] for c in ca.status()["certificates"]}
    assert origins == {"ui", "dropped"}


def test_the_same_root_dropped_twice_is_listed_once(cadir):
    pem = _cert("Corp Root")
    (cadir / "a.crt").write_text(pem, encoding="utf-8")
    (cadir / "b.pem").write_text(pem, encoding="utf-8")
    assert len(ca.status()["certificates"]) == 1


def test_nothing_supplied_lists_nothing(cadir):
    st = ca.status()
    assert st["certificates"] == []
    assert st["others_in_bundle"] == 0
