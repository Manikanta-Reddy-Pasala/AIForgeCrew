"""MCP marketplace registry + one-click install + endpoint merge."""
from __future__ import annotations

import pytest


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_MCP_ENDPOINTS", raising=False)
    return tmp_path


def test_catalog_loads_and_flags_installable(cfg):
    from aiforge_core.config import mcp_registry
    cat = mcp_registry.load_catalog()
    assert cat, "catalog should ship at least one entry"
    ids = {c["id"] for c in cat}
    assert "context7" in ids
    assert all("installable" in c for c in cat)
    assert next(c for c in cat if c["id"] == "context7")["installable"] is True


def test_install_from_catalog_and_list(cfg):
    from aiforge_core.config import mcp_registry
    row = mcp_registry.install_from_catalog("context7")
    assert row["catalog_id"] == "context7"
    assert row["enabled"] is True
    assert row["api_key_set"] is False
    listed = mcp_registry.list_servers()
    assert any(s["id"] == row["id"] for s in listed)
    # secrets never leak in the public projection
    assert "api_key" not in listed[0]


def test_custom_requires_url(cfg):
    from aiforge_core.config import mcp_registry
    with pytest.raises(ValueError):
        mcp_registry.install_from_catalog("custom-http")  # no url
    row = mcp_registry.install_from_catalog(
        "custom-http", url="https://my.example/mcp", name="Mine")
    assert row["url"] == "https://my.example/mcp"


def test_enabled_endpoints_merges_into_client(cfg):
    from aiforge_core.config import mcp_registry
    from aiforge_core.runtime.tools import mcp_client
    mcp_registry.install_from_catalog("context7")
    eps = mcp_client._load_endpoints()
    assert "Context7" in eps
    assert eps["Context7"].startswith("https://")


def test_disable_hides_from_endpoints(cfg):
    from aiforge_core.config import mcp_registry
    from aiforge_core.runtime.tools import mcp_client
    row = mcp_registry.install_from_catalog("context7")
    mcp_registry.update_server(row["id"], enabled=False)
    assert "Context7" not in mcp_client._load_endpoints()


def test_remove_server(cfg):
    from aiforge_core.config import mcp_registry
    row = mcp_registry.install_from_catalog("context7")
    assert mcp_registry.remove_server(row["id"]) is True
    assert mcp_registry.remove_server(row["id"]) is False


def test_add_rejects_stdio_transport(cfg):
    from aiforge_core.config import mcp_registry
    with pytest.raises(ValueError):
        mcp_registry.add_server(name="x", url="npx server-foo", transport="stdio")
