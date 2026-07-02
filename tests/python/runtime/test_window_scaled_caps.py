"""Fix A2 — the fixed-char section caps must be WINDOW-RELATIVE so a 256K
window is actually used. At 32K each cap == its floor (byte-identical to today);
at 256K each is materially larger; an explicit env override wins at any window.
"""
from __future__ import annotations

import importlib

import pytest

from aiforge_core.runtime import chat_agent as ca
from aiforge_core.runtime import mentions as mn
from aiforge_core.runtime import parallel_stages as ps


@pytest.fixture(autouse=True)
def _cfg(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFORGE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AIFORGE_AUTODETECT_CTX", "0")
    for v in ("AIFORGE_LOCAL_CTX_WINDOW", "AIFORGE_CTX_SECTION_CHARS",
              "AIFORGE_MENTIONS_TOTAL_CHARS", "AIFORGE_REPOMAP_MAX_CHARS"):
        monkeypatch.delenv(v, raising=False)
    import aiforge_core.config.runtime_settings as rsmod
    importlib.reload(rsmod)
    yield
    importlib.reload(rsmod)


def _reload_rs():
    import aiforge_core.config.runtime_settings as rsmod
    importlib.reload(rsmod)


# (getter, floor, env var, frac)
_CAPS = [
    (ps._ctx_section_cap, 8000, "AIFORGE_CTX_SECTION_CHARS", 0.03),
    (mn._mentions_total_chars, 48000, "AIFORGE_MENTIONS_TOTAL_CHARS", 0.10),
    (ca._repomap_max_chars, 6000, "AIFORGE_REPOMAP_MAX_CHARS", 0.02),
]


@pytest.mark.parametrize("getter,floor,env,frac", _CAPS)
def test_cap_equals_floor_at_32k(monkeypatch, getter, floor, env, frac):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "32768")
    _reload_rs()
    # 32768*4*frac <= floor for all three, so the floor binds → unchanged.
    assert getter() == floor


@pytest.mark.parametrize("getter,floor,env,frac", _CAPS)
def test_cap_grows_at_256k(monkeypatch, getter, floor, env, frac):
    monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", "262144")
    _reload_rs()
    val = getter()
    assert val > floor
    # Roughly the fraction of the window (chars).
    assert val == int(262144 * 4 * frac)


@pytest.mark.parametrize("getter,floor,env,frac", _CAPS)
def test_explicit_env_override_wins(monkeypatch, getter, floor, env, frac):
    monkeypatch.setenv(env, "1234")
    for win in ("32768", "262144"):
        monkeypatch.setenv("AIFORGE_LOCAL_CTX_WINDOW", win)
        _reload_rs()
        assert getter() == 1234           # verbatim, not scaled, at any window
