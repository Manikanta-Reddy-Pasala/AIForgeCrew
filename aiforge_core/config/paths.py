"""Where AIForge keeps its state — asked once, answered here.

``os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")`` was open-coded in ~40
places, and they did not agree: most wrapped it in ``expanduser``, several did
not. That difference is invisible until someone sets
``AIFORGE_CONFIG_DIR=~/foo`` — the shell does not expand it inside a variable,
so the careful call sites land in ``$HOME/foo`` and the careless ones create a
literal ``./~/foo`` directory next to wherever the process happened to start.
Half the databases in one place, half in another.

Import-light on purpose (stdlib only): this is imported by config, memory,
runtime and the API, so anything heavier would build an import cycle.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_CONFIG_DIR = "~/.aiforge"


def config_dir() -> Path:
    """The config/state directory, absolute and with ``~`` expanded.

    Expands the ENV VALUE as well as the default — that is the half the
    open-coded copies kept getting wrong.
    """
    raw = (os.environ.get("AIFORGE_CONFIG_DIR") or "").strip() or DEFAULT_CONFIG_DIR
    return Path(os.path.expanduser(raw))


def config_path(*parts: str) -> Path:
    """A path inside the config dir. ``config_path("memory")`` etc."""
    return config_dir().joinpath(*parts)


def ensure_config_dir(*parts: str) -> Path:
    """Like :func:`config_path`, but the directory exists when it returns."""
    p = config_path(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


__all__ = ["config_dir", "config_path", "ensure_config_dir", "DEFAULT_CONFIG_DIR"]
