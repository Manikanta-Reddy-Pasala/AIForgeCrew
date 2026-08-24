"""Config files are owner-only, because they hold credentials.

`agent_config` persists an `api_key` (agent_config/_persist.py), and everything
under AIFORGE_CONFIG_DIR is published through `_atomic`. That writer used to
widen `mkstemp`'s 0600 back out to `0o666 & ~umask` — 0644 under the usual
umask — on the reasoning that a plain `open()` would have done the same. For a
credential store that is the wrong benchmark: it meant every local user on the
machine could read the operator's API keys.
"""
import importlib
import os
import stat

import pytest


@pytest.fixture
def atomic(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CONFIG_MODE", raising=False)
    from aiforge_core.config import _atomic
    return importlib.reload(_atomic), tmp_path


def _mode(p) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_a_written_config_is_not_readable_by_other_users(atomic):
    mod, tmp = atomic
    p = tmp / "agent_config.json"
    mod.write_bytes(p, b'{"api_key": "sk-secret"}')
    m = _mode(p)
    assert not m & stat.S_IRGRP, f"group-readable: {oct(m)}"
    assert not m & stat.S_IROTH, f"world-readable: {oct(m)}"
    assert m == 0o600, oct(m)


def test_the_umask_cannot_widen_it(monkeypatch, tmp_path):
    """The old default was derived FROM the umask, so a permissive umask made
    credentials world-readable. The mode is now fixed, not inherited."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CONFIG_MODE", raising=False)
    old = os.umask(0o000)          # maximally permissive
    try:
        from aiforge_core.config import _atomic
        mod = importlib.reload(_atomic)
        p = tmp_path / "x.json"
        mod.write_bytes(p, b"{}")
        assert _mode(p) == 0o600, oct(_mode(p))
    finally:
        os.umask(old)


def test_an_operator_can_widen_it_deliberately(monkeypatch, tmp_path):
    """A deployment that genuinely needs group reads can ask for it — but it
    has to ask."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CONFIG_MODE", "0640")
    from aiforge_core.config import _atomic
    mod = importlib.reload(_atomic)
    p = tmp_path / "y.json"
    mod.write_bytes(p, b"{}")
    assert _mode(p) == 0o640, oct(_mode(p))


def test_a_nonsense_mode_falls_back_to_owner_only(monkeypatch, tmp_path):
    """A typo in the override must not silently open the file up."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_CONFIG_MODE", "not-octal")
    from aiforge_core.config import _atomic
    mod = importlib.reload(_atomic)
    p = tmp_path / "z.json"
    mod.write_bytes(p, b"{}")
    assert _mode(p) == 0o600, oct(_mode(p))


def test_the_real_settings_writer_goes_through_it(monkeypatch, tmp_path):
    """End to end: the path an operator's API key actually takes."""
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("AIFORGE_CONFIG_MODE", raising=False)
    from aiforge_core.config import _atomic, _filecache
    importlib.reload(_atomic)
    importlib.reload(_filecache)
    p = tmp_path / "settings.json"
    _filecache.write_json(p, {"api_key": "sk-secret"})
    assert _mode(p) == 0o600, oct(_mode(p))
