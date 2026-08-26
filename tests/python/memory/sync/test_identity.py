"""Peer identity and the version stamp applied to mutable nodes."""
from __future__ import annotations

from pathlib import Path


def test_self_id_defaults_to_hostname_slug(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AIFORGE_PEER_ID", raising=False)
    monkeypatch.setattr("socket.gethostname", lambda: "My Laptop.local")
    from aiforge_core.memory.sync import identity

    assert identity.self_id() == "my-laptop-local"


def test_self_id_env_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    from aiforge_core.memory.sync import identity

    assert identity.self_id() == "nuc"


def test_stamp_sets_origin_on_first_write(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    from aiforge_core.memory.sync import identity

    meta = identity.stamp({"title": "T"})
    assert meta["origin"] == "nuc"
    assert meta["rev"] == 1
    assert meta["updated_by"] == "nuc"


def test_stamp_bumps_rev_and_records_writer(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "ms")
    from aiforge_core.memory.sync import identity

    meta = identity.stamp({"title": "T", "origin": "nuc", "rev": 46, "updated_by": "nuc"})
    assert meta["origin"] == "nuc"      # origin never changes hands
    assert meta["rev"] == 47
    assert meta["updated_by"] == "ms"


def test_save_node_stamps_frontmatter(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_MEMORY_MD_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AIFORGE_PEER_ID", "nuc")
    from aiforge_core.memory.okf import nodes, store

    res = store.save_node("learning", None, {"title": "L"}, "body")
    node = nodes.parse_node(Path(res["path"]).read_text(encoding="utf-8"))
    assert node["meta"]["origin"] == "nuc"
    assert node["meta"]["rev"] == 1
    assert node["meta"]["updated_by"] == "nuc"


def test_learning_the_admin_id_does_not_forget_the_saved_admin_url(tmp_path, monkeypatch):
    """Both live in admin.json. Writing one as a whole-file replace dropped the
    other, so a machine forgot its admin the moment it learned who it was."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg"))
    for k in ("AIFORGE_ADMIN_URL", "AIFORGE_ROLE", "AIFORGE_ADMIN_ID"):
        monkeypatch.delenv(k, raising=False)
    from aiforge_core.memory.sync import role

    role.set_admin_url("http://nuc:8799")
    role.remember_admin_id("hub")

    assert role.admin_url() == "http://nuc:8799"
    assert role.admin_id() == "hub"
    assert role.role() == "spoke"


def test_saving_an_admin_url_does_not_forget_the_learned_id(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path / "cfg2"))
    for k in ("AIFORGE_ADMIN_URL", "AIFORGE_ROLE", "AIFORGE_ADMIN_ID"):
        monkeypatch.delenv(k, raising=False)
    from aiforge_core.memory.sync import role

    role.set_admin_url("http://nuc:8799")
    role.remember_admin_id("hub")
    role.set_admin_url("http://other:8799")

    assert role.admin_id() == "hub"
