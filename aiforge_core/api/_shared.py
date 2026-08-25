"""Small cross-cutting helpers shared by api.py and the api/routes/ modules.

Kept dependency-light (stdlib + env) so route modules can import it without
pulling api.py back in (which would be circular). Add a helper here only when
it's used by BOTH api.py and a route module.
"""
from __future__ import annotations

import os
import re
import threading

from aiforge_core.config.paths import config_dir


def env_truthy(name: str) -> bool:
    """True when env var ``name`` is set to a truthy string (1/true/yes/on)."""
    return str(os.environ.get(name, "")).strip().lower() in (
        "1", "true", "yes", "on")


def _ticket_files_base():
    """Stable, PERSISTENT base dir for ticket attachments.

    Must not depend on ``AIFORGE_REPO_ROOT``: the runner rebinds it per
    ticket, AND in Docker it is unset → defaults to ``HOME/aiforge_workspace``,
    which is NOT a mounted volume, so every container recreate wiped uploads
    (the "image not found" 404). Resolution order:
      1. ``AIFORGE_TICKET_FILES_DIR``        explicit override
      2. ``{AIFORGE_CONFIG_DIR}/ticket-files`` (a persistent volume in Docker)
      3. ``{AIFORGE_REPO_ROOT|~/aiforge_workspace}/.aiforge/ticket-files``
         (repo-relative for a local checkout)
    """
    import os as _os
    from pathlib import Path as _Path
    explicit = _os.environ.get("AIFORGE_TICKET_FILES_DIR", "").strip()
    if explicit:
        return _Path(explicit).expanduser().resolve()
    cfg = _os.environ.get("AIFORGE_CONFIG_DIR", "").strip()
    if cfg:
        return (_Path(cfg).expanduser() / "ticket-files").resolve()
    root = _Path(_os.path.expanduser(_os.environ.get(
        "AIFORGE_REPO_ROOT", "~/aiforge_workspace",
    ))).resolve()
    return (root / ".aiforge" / "ticket-files").resolve()


# runtime.env — UI-persisted toggles restored into the process env on boot.
# The path + lock live here (shared) so both api.py's startup loader and the
# runtime route module's _persist_env write/read the same single location.
_RUNTIME_ENV_PATH = os.path.expanduser(
    os.environ.get("AIFORGE_RUNTIME_ENV", os.path.join(
        str(config_dir()), "runtime.env"))
)
_RUNTIME_ENV_LOCK = threading.Lock()


def _persist_env(key: str, value: str) -> None:
    """Upsert ``key=value`` into runtime.env so it survives a restart (the API
    reloads it with a plain KEY=VALUE parser at startup — see _load_runtime_env;
    it is NOT shell-sourced). Line-replace, order-preserving. Creates the file
    + dir when absent. Sanitises so the file stays a clean KEY=VALUE store:
    keys restricted to env-name chars; CR/LF stripped from the value (a newline
    could otherwise smuggle a second assignment into the file)."""
    key = re.sub(r"[^A-Za-z0-9_]", "", str(key))
    if not key:
        return
    value = str(value).replace("\r", " ").replace("\n", " ")
    with _RUNTIME_ENV_LOCK:                       # serialize concurrent PUTs
        try:
            os.makedirs(os.path.dirname(_RUNTIME_ENV_PATH), exist_ok=True)
        except Exception:  # noqa: BLE001
            pass
        lines: list[str] = []
        if os.path.isfile(_RUNTIME_ENV_PATH):
            with open(_RUNTIME_ENV_PATH) as _f:
                lines = _f.read().splitlines()
        found = False
        for i, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[i] = f"{key}={value}"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}")
        with open(_RUNTIME_ENV_PATH, "w") as f:
            f.write("\n".join(lines) + "\n")


__all__ = ["env_truthy", "_ticket_files_base", "_persist_env",
           "_RUNTIME_ENV_PATH", "_RUNTIME_ENV_LOCK"]
