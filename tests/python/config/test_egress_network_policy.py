"""The box READS the network allowlist (isolated mode) from the controller's
volume, mounted read-only: Settings shows it and AIForge's own web tools
follow it. Outside isolated mode there is none."""
from __future__ import annotations

import json

from aiforge_core.config import egress_hosts


def test_not_isolated_means_no_network_list(monkeypatch):
    monkeypatch.delenv("AIFORGE_EGRESS_POLICY_RO", raising=False)
    assert egress_hosts.network_policy() is None
    assert egress_hosts.network_hosts() == set()
    assert egress_hosts.describe()["network"] is None


def test_isolated_reads_the_list(monkeypatch, tmp_path):
    (tmp_path / "hosts.json").write_text(json.dumps({"hosts": ["pypi.org", "a.example"]}))
    monkeypatch.setenv("AIFORGE_EGRESS_POLICY_RO", str(tmp_path))
    assert egress_hosts.network_policy() == {"isolated": True, "hosts": ["pypi.org", "a.example"]}
    assert {"pypi.org", "a.example"} <= egress_hosts.allowed_hosts()
    assert "pypi.org" not in egress_hosts.write_hosts()      # reading, not a write destination


def test_isolated_with_no_list_yet_allows_nothing_extra(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_EGRESS_POLICY_RO", str(tmp_path))
    assert egress_hosts.network_policy() == {"isolated": True, "hosts": []}
    (tmp_path / "hosts.json").write_text("{broken")
    assert egress_hosts.network_hosts() == set()
