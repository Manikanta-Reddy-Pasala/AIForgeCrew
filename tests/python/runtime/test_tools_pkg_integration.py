"""End-to-end smoke: editor + bash + finish together in a temp workspace.

No ADK loop required — exercises the tools directly to confirm they
compose. Tmux-aware tests are skipped on dev boxes without tmux.
"""
from __future__ import annotations

import shutil
import sys

import pytest


def test_create_then_view_works(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    from aiforge_core.runtime.tools.editor import editor

    create = editor("create", "hello.py", file_text="print('hi')\n")
    assert create["ok"], create

    view = editor("view", "hello.py")
    assert view["ok"], view
    assert "print('hi')" in view["content"]


def test_finish_signal_terminates_doer():
    from aiforge_core.runtime.tools.cognition import finish

    out = finish("created hello.py and verified", _agent_role="doer")
    assert out["ok"]
    assert out["terminate"] is True


def test_full_create_run_finish_chain(tmp_path, monkeypatch):
    """Full chain: editor create → fallback bash (no tmux) executes the
    file → finish signals done."""
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir(parents=True, exist_ok=True)

    from aiforge_core.runtime.tools import bash as bm
    from aiforge_core.runtime.tools.cognition import finish
    from aiforge_core.runtime.tools.editor import editor

    # Force fallback to keep this test platform-portable.
    monkeypatch.setattr(bm, "_tmux_available", lambda: False)

    create = editor(
        "create", "hello.py",
        file_text="print('hi from tools pkg')\n",
    )
    assert create["ok"], create

    out = bm.bash(f"{sys.executable} hello.py")
    assert out["ok"], out
    assert "hi from tools pkg" in out["stdout"]

    done = finish("hello.py created and executed", _agent_role="doer")
    assert done["ok"]
    assert done["terminate"] is True


pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None, reason="tmux not installed",
)


@pytestmark_tmux
def test_tmux_persistent_session_smoke(tmp_path, monkeypatch):
    monkeypatch.setenv("AIFORGE_REPO_ROOT", str(tmp_path))
    from aiforge_core.runtime.tools import bash as bm

    run_id = "smoke-tmux"
    try:
        bm.bash("export SMOKE_VAR=alive", _run_id=run_id)
        out = bm.bash("echo $SMOKE_VAR", _run_id=run_id)
        assert out["ok"], out
        assert out["stdout"].strip() == "alive"
    finally:
        bm.destroy_session(run_id)
