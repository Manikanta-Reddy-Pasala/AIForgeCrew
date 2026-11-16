"""Per-host workflows, loaded from the user config folder.

Project-specific workflows do not ship in the product package. A host that
needs one drops a Python module into ``$AIFORGE_CONFIG_DIR/workflows/``
(default ``~/.aiforge/workflows/``); each ``*.py`` file there is imported once
at package load as ``aiforge_user_workflows.<stem>`` and registers itself with
:func:`aiforge_core.workflows.register`. A handler path inside such a module
is therefore ``"aiforge_user_workflows.<stem>:<function>"``.

A module that fails to import is logged and skipped — one broken file must
not take the workflow registry (or the API) down with it.
"""
from __future__ import annotations

import importlib.util
import logging
import os
import sys
import types
from pathlib import Path

log = logging.getLogger("aiforge.workflows")

PACKAGE = "aiforge_user_workflows"


def user_workflow_dir() -> Path:
    base = os.environ.get("AIFORGE_CONFIG_DIR") or os.path.expanduser("~/.aiforge")
    return Path(base) / "workflows"


def _ensure_package(folder: Path) -> None:
    pkg = sys.modules.get(PACKAGE)
    if pkg is None:
        pkg = types.ModuleType(PACKAGE)
        pkg.__path__ = []  # type: ignore[attr-defined]
        sys.modules[PACKAGE] = pkg
    if str(folder) not in pkg.__path__:  # type: ignore[attr-defined]
        pkg.__path__.append(str(folder))  # type: ignore[attr-defined]


def load_user_workflows(folder: Path | None = None) -> list[str]:
    """Import every ``*.py`` in the user workflow folder; return the loaded
    module names. Missing folder → nothing loaded."""
    folder = folder or user_workflow_dir()
    if not folder.is_dir():
        return []
    _ensure_package(folder)
    loaded: list[str] = []
    for path in sorted(folder.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = f"{PACKAGE}.{path.stem}"
        if name in sys.modules:
            loaded.append(name)
            continue
        try:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            loaded.append(name)
        except Exception as exc:  # noqa: BLE001 — a bad plugin must not break boot
            sys.modules.pop(name, None)
            log.warning("workflows: skipping user workflow %s: %s", path, exc)
    return loaded


__all__ = ["PACKAGE", "load_user_workflows", "user_workflow_dir"]
