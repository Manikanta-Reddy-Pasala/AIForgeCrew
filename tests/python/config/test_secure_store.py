"""Every credential this install owns lives in one 0700 folder.

They used to sit in the config root beside caches, catalogs and health
snapshots — nobody can back that up selectively, exclude it from a sync, or
audit it, and the one time it mattered a live api_key sat at 0644 for months
because a write-time mode fix cannot reach a file nothing rewrites.

The two properties worth pinning are that the migration is a MOVE (a copy would
leave the secret exactly where it was and fix nothing) and that every consumer
follows without knowing the layout changed.
"""
from __future__ import annotations

import json
import os

import pytest

from aiforge_core.config import secure_store as ss


@pytest.fixture(autouse=True)
def _config(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    for var in ("AIFORGE_SECURITY_DIR", "AIFORGE_SECURE_STORE",
                "AIFORGE_RUNTIME_ENV"):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _legacy(tmp_path, name: str, text: str = "{}") -> None:
    p = tmp_path / name
    p.write_text(text)
    p.chmod(0o644)


# ── the move ────────────────────────────────────────────────────────────────

def test_a_legacy_credential_file_is_moved_not_copied(_config):
    """A copy is strictly worse than doing nothing: the secret would then exist
    in two places, one of them exactly as exposed as before."""
    _legacy(_config, "agent_config.json", '{"doer": {"api_key": "sk-live"}}')

    out = ss.migrate_all()

    assert out["moved"] == ["agent_config.json"]
    assert not (_config / "agent_config.json").exists()
    moved = _config / "security" / "agent_config.json"
    assert json.loads(moved.read_text())["doer"]["api_key"] == "sk-live"


def test_the_folder_is_owner_only_and_so_are_its_files(_config):
    _legacy(_config, "integrations.json")
    ss.migrate_all()
    assert oct((_config / "security").stat().st_mode & 0o777) == "0o700"
    assert oct((_config / "security" / "integrations.json").stat().st_mode
               & 0o777) == "0o600"


def test_migration_is_idempotent(_config):
    _legacy(_config, "agent_config.json", '{"a": 1}')
    assert ss.migrate_all()["moved"] == ["agent_config.json"]
    assert ss.migrate_all()["moved"] == []
    assert json.loads((_config / "security" / "agent_config.json").read_text())


def test_a_file_already_in_the_folder_is_never_overwritten(_config):
    """A downgrade, or a stale copy left by a crash, must not clobber the live
    credential."""
    (_config / "security").mkdir()
    (_config / "security" / "agent_config.json").write_text('{"live": true}')
    _legacy(_config, "agent_config.json", '{"stale": true}')
    ss.migrate_all()
    assert json.loads(
        (_config / "security" / "agent_config.json").read_text()) == {"live": True}


def test_nothing_to_move_is_not_an_error(_config):
    assert ss.migrate_all()["moved"] == []


# ── consumers follow without knowing ────────────────────────────────────────

def test_the_agent_config_reader_resolves_into_the_folder(_config):
    from aiforge_core.config.agent_config import _state
    assert str(_state._path()).endswith("security/agent_config.json")


def test_the_integrations_reader_resolves_into_the_folder(_config):
    from aiforge_core.config import integrations
    assert str(integrations._path()).endswith("security/integrations.json")


def test_the_mcp_registry_resolves_into_the_folder(_config):
    from aiforge_core.config import mcp_registry
    assert str(mcp_registry._path()).endswith("security/mcp_servers.json")


def test_reading_a_legacy_file_through_a_consumer_migrates_it(_config):
    """The move happens on first RESOLUTION, so an install that never calls
    migrate_all (a CLI, a test, a one-shot script) still ends up consolidated
    rather than writing a second copy into the folder."""
    _legacy(_config, "integrations.json", '{"jira": {"token": "t"}}')
    from aiforge_core.config import integrations
    data = integrations.load_all()
    assert data["jira"]["token"] == "t"
    assert not (_config / "integrations.json").exists()
    assert (_config / "security" / "integrations.json").is_file()


def test_a_write_lands_in_the_folder_when_nothing_existed(_config):
    from aiforge_core.config import integrations
    integrations.set_("jira", {"base_url": "https://jira.corp", "token": "t"})
    assert (_config / "security" / "integrations.json").is_file()
    assert not (_config / "integrations.json").exists()


# ── the switches ────────────────────────────────────────────────────────────

def test_the_rollback_switch_keeps_every_file_where_it_is(_config, monkeypatch):
    monkeypatch.setenv("AIFORGE_SECURE_STORE", "0")
    _legacy(_config, "agent_config.json", '{"a": 1}')
    assert ss.migrate_all()["moved"] == []
    assert (_config / "agent_config.json").is_file()
    assert str(ss.secure_path("agent_config.json")) == str(
        _config / "agent_config.json")


def test_the_folder_can_live_elsewhere(_config, monkeypatch, tmp_path):
    vault = tmp_path / "vault"
    monkeypatch.setenv("AIFORGE_SECURITY_DIR", str(vault))
    _legacy(_config, "agent_config.json", '{"a": 1}')
    ss.migrate_all()
    assert (vault / "agent_config.json").is_file()


def test_reading_never_creates_the_directory(_config):
    """A read that mkdirs turns "is anything configured?" into a side effect,
    and raises on a config dir the process cannot write."""
    ss.security_dir()
    assert not (_config / "security").exists()


# ── the boot repair still covers them ───────────────────────────────────────

def test_the_permission_repair_reaches_the_new_location(_config):
    from aiforge_core.config import permissions
    _legacy(_config, "agent_config.json", '{"a": 1}')
    ss.migrate_all()
    moved = _config / "security" / "agent_config.json"
    moved.chmod(0o644)                      # as a downgrade or an editor leaves it

    out = permissions.repair()

    assert oct(moved.stat().st_mode & 0o777) == "0o600"
    assert any("agent_config" in f for f in out["fixed"]), out


def test_the_legacy_location_is_still_repaired(_config, monkeypatch):
    """A box running with the rollback switch, or one that has not booted since
    the move, must not quietly stop being covered."""
    monkeypatch.setenv("AIFORGE_SECURE_STORE", "0")
    from aiforge_core.config import permissions
    _legacy(_config, "integrations.json")
    permissions.repair()
    assert oct((_config / "integrations.json").stat().st_mode & 0o777) == "0o600"


def test_paths_reports_what_is_in_the_folder(_config):
    _legacy(_config, "agent_config.json", '{"a": 1}')
    ss.migrate_all()
    rows = ss.paths()
    assert rows["agent_config.json"]["exists"] is True
    assert rows["agent_config.json"]["mode"] == "0o600"
    assert rows["runtime.env"]["exists"] is False


def test_every_named_file_is_one_we_actually_write():
    """A name in this list that nothing writes is a migration that never runs;
    a credential file missing from it keeps sitting in the open."""
    assert set(ss.SECRET_FILES) >= {"agent_config.json", "integrations.json",
                                    "mcp_servers.json", "runtime.env"}


def test_a_symlinked_legacy_file_is_left_alone(_config):
    """Moving a symlink would move the LINK and leave the target behind — and
    an operator who symlinked a credential in from elsewhere meant it."""
    real = _config / "elsewhere.json"
    real.write_text('{"a": 1}')
    os.symlink(real, _config / "agent_config.json")
    assert ss.migrate_all()["moved"] == []
    assert (_config / "agent_config.json").is_symlink()
