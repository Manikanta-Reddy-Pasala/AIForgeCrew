from __future__ import annotations

from pathlib import Path

import pytest

from aiforge_core.mem import MemBus
from aiforge_core.permissions import PermissionDenied


def test_project_memory_write_acl(tmp_path: Path) -> None:
    bus = MemBus(base_dir=tmp_path)
    # em + sr-architect are the only allowed project writers per §6.1.
    for bad_role in ("tester", "sr-developer"):
        with pytest.raises(PermissionDenied):
            bus.remember(bad_role, "project", "bad")


def test_own_memory_write_allowed_for_all_roles(tmp_path: Path, monkeypatch) -> None:
    bus = MemBus(base_dir=tmp_path)
    # Patch _run so ACL-only test doesn't need a real mempalace install.
    monkeypatch.setattr(MemBus, "_run", lambda self, args, check=True: "")
    for role in ("em", "tester", "sr-developer", "sr-architect"):
        bus.remember(role, "own", "note")


def test_unknown_role_rejected(tmp_path: Path) -> None:
    bus = MemBus(base_dir=tmp_path)
    with pytest.raises(PermissionDenied):
        bus.remember("hacker", "own", "x")


def test_unknown_scope_rejected(tmp_path: Path) -> None:
    bus = MemBus(base_dir=tmp_path)
    with pytest.raises(ValueError):
        bus.remember("em", "everywhere", "x")


def test_search_palace_paths(tmp_path: Path) -> None:
    bus = MemBus(base_dir=tmp_path)
    # scope='auto' returns project + agent own.
    paths = bus._search_palaces("tester", "auto")
    assert [p.name for p in paths] == ["project", "tester"]
