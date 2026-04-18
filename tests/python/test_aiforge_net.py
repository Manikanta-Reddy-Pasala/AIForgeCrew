from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_core.net import FetchDenied, _host_allowed, _load_allowlist, fetch_url

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_allowlist_loads() -> None:
    rules = _load_allowlist(REPO_ROOT)
    assert "github.com" in rules["allow_domains"]
    assert rules["allow_localhost"] is False


def test_host_allowed_exact() -> None:
    assert _host_allowed("github.com", ["github.com"], False)


def test_host_allowed_wildcard() -> None:
    assert _host_allowed("numpy.readthedocs.io", ["*.readthedocs.io"], False)
    assert not _host_allowed("readthedocs.io", ["*.readthedocs.io"], False)


def test_host_denied_by_default() -> None:
    assert not _host_allowed("evil.example.com", ["github.com"], False)


def test_scheme_filter(tmp_path: Path) -> None:
    with pytest.raises(FetchDenied, match="scheme"):
        fetch_url(REPO_ROOT, {"url": "ftp://github.com"})


def test_method_filter() -> None:
    with pytest.raises(FetchDenied, match="method"):
        fetch_url(REPO_ROOT, {"url": "https://github.com", "method": "DELETE"})


def test_denied_host(tmp_path: Path) -> None:
    with pytest.raises(FetchDenied, match="not in network-allowlist"):
        fetch_url(REPO_ROOT, {"url": "https://evil.example.com/x"})


def test_private_ip_denied() -> None:
    # localhost / 127.0.0.1 — allow_localhost=False in our default policy
    assert not _host_allowed("127.0.0.1", ["127.0.0.1"], allow_localhost=False)
    assert _host_allowed("127.0.0.1", ["127.0.0.1"], allow_localhost=True)
