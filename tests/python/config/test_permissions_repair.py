"""The config dir is repaired on boot, not only written correctly.

`_atomic` publishes new files at 0600 and its docstring names
`agent_config.json`'s api_key as the reason. But a write-time mode never
touches a file that already exists — and a credential file is exactly the kind
written once and read for years. On the machine where this was found,
`agent_config.json` still held a live api_key at 0644 months after that
hardening, because nothing had rewritten it.
"""
from __future__ import annotations

import os
import stat

import pytest

from aiforge_core.config import permissions


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


@pytest.fixture
def loose_dir(tmp_path):
    """A config dir as it looks on a box that predates the hardening."""
    (tmp_path / "agent_config.json").write_text('{"_default":{"api_key":"live"}}')
    (tmp_path / "chat.db").write_text("transcripts")
    (tmp_path / "memory").mkdir()
    (tmp_path / "memory" / "note.md").write_text("a note")
    for p in (tmp_path, tmp_path / "agent_config.json", tmp_path / "chat.db"):
        os.chmod(p, 0o644 if p.is_file() else 0o755)
    os.chmod(tmp_path / "memory", 0o755)
    yield tmp_path
    # Hand the tree back permissive or pytest's tmp_path cleanup cannot walk
    # it — the same trap the ticket-attachment fixture hit: a test that tightens
    # permissions must loosen them again, or it breaks the NEXT run's teardown.
    for root, dirs, files in os.walk(tmp_path):
        for name in dirs + files:
            try:
                os.chmod(os.path.join(root, name), 0o755)
            except OSError:
                pass
    os.chmod(tmp_path, 0o755)


def test_an_existing_world_readable_credential_is_tightened(loose_dir):
    assert _mode(loose_dir / "agent_config.json") == 0o644   # control
    permissions.repair(loose_dir)
    assert _mode(loose_dir / "agent_config.json") == 0o600


def test_the_databases_are_covered_too(loose_dir):
    """chat.db is every transcript and memory.db is the note tree — neither is
    written through _atomic, so neither was ever in scope of the 0600 rule."""
    permissions.repair(loose_dir)
    assert _mode(loose_dir / "chat.db") == 0o600
    assert _mode(loose_dir / "memory") == 0o700
    assert _mode(loose_dir) == 0o700


def test_it_is_idempotent_and_reports_what_it_changed(loose_dir):
    first = permissions.repair(loose_dir)
    assert first["fixed"], "nothing reported on a dir that needed fixing"
    assert permissions.repair(loose_dir)["fixed"] == []


def test_it_never_loosens_a_file_that_is_already_private(loose_dir):
    os.chmod(loose_dir / "agent_config.json", 0o400)
    permissions.repair(loose_dir)
    assert _mode(loose_dir / "agent_config.json") == 0o400


def test_a_deployment_can_ask_for_group_reads(loose_dir, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_MODE", "0640")
    permissions.repair(loose_dir)
    assert _mode(loose_dir / "agent_config.json") == 0o640
    # a group that may read the files needs to traverse the directory
    assert _mode(loose_dir) & 0o050 == 0o050


def test_the_pass_can_be_turned_off(loose_dir, monkeypatch):
    monkeypatch.setenv("AIFORGE_SKIP_PERM_REPAIR", "1")
    assert permissions.repair(loose_dir)["skipped"]
    assert _mode(loose_dir / "agent_config.json") == 0o644


def test_a_missing_dir_is_not_an_error(tmp_path):
    assert permissions.repair(tmp_path / "nope")["fixed"] == []


def test_an_unchmodable_file_does_not_break_boot(loose_dir, monkeypatch):
    """A root-owned leftover from an older deployment is exactly what this will
    meet in the field (the ticket-files dir on the server was one). It must be
    logged and stepped over, never raised."""
    import pathlib

    real = pathlib.Path.chmod

    def _boom(self, mode, **kw):
        if self.name == "agent_config.json":
            raise PermissionError(1, "Operation not permitted")
        return real(self, mode, **kw)

    monkeypatch.setattr(pathlib.Path, "chmod", _boom)
    out = permissions.repair(loose_dir)          # must not raise
    assert "agent_config.json -> 0o600" not in out["fixed"]


def test_a_junk_config_mode_falls_back_to_owner_only(loose_dir, monkeypatch):
    """A typo in AIFORGE_CONFIG_MODE must not silently widen a credential
    file — an unparseable mode falls back to 0600, not to the umask."""
    monkeypatch.setenv("AIFORGE_CONFIG_MODE", "not-a-mode")
    permissions.repair(loose_dir)
    assert _mode(loose_dir / "agent_config.json") == 0o600


def test_a_file_that_cannot_be_stat_ed_is_skipped(loose_dir, monkeypatch):
    """A dangling entry or a race must not raise out of the boot path."""
    import pathlib

    real = pathlib.Path.lstat

    def _boom(self):
        if self.name == "chat.db":
            raise OSError(5, "I/O error")
        return real(self)

    monkeypatch.setattr(pathlib.Path, "lstat", _boom)
    out = permissions.repair(loose_dir)          # must not raise
    assert "chat.db -> 0o600" not in out["fixed"]


def test_a_symlink_is_never_chmodded(loose_dir):
    """chmod follows a symlink, so repairing one would change the mode of
    whatever it points at — possibly a file outside the config dir."""
    target = loose_dir.parent / "outside.json"
    target.write_text("{}")
    os.chmod(target, 0o644)
    (loose_dir / "integrations.json").symlink_to(target)
    permissions.repair(loose_dir)
    assert _mode(target) == 0o644, "repair followed a symlink out of the dir"
