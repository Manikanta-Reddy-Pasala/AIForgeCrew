"""The stateful tool resources ONE agent run owns — keyed to it, torn down with it.

bash keeps a tmux session, the browser a context, IPython a kernel. Each
reads a per-run id from a contextvar; unset, they fall back to a shared
"default". The ticket driver keyed them; team chat never did, so every team
run shared one shell — a run on repo B executed its commands in the directory
repo A's run had cd'd into — and nothing was cleaned up afterwards. Both
drivers now call these two functions around every run.
"""
from __future__ import annotations

import importlib
import logging

log = logging.getLogger("aiforge.run_resources")

_KEYED = (("bash", "set_run_id"), ("browser", "set_run_id"),
          ("ipython_kernel", "set_run_id"))
_TEARDOWN = (("aiforge_core.runtime.tools.bash", "destroy_session"),
             ("aiforge_core.runtime.tools.browser", "destroy_context"),
             ("aiforge_core.runtime.tools.ipython_kernel", "destroy_kernel"),
             ("aiforge_core.runtime.docker_sandbox", "destroy_container"))


def key_stateful_tools(run_id: str) -> None:
    """Key bash / browser / IPython to ``run_id`` for the calling context (and
    every thread that copies it), so :func:`destroy_run_resources` matches."""
    for mod, fn in _KEYED:
        try:
            getattr(importlib.import_module(
                f"aiforge_core.runtime.tools.{mod}"), fn)(run_id)
        except Exception:  # noqa: BLE001 — a missing tool never blocks a run
            pass


def destroy_run_resources(run_id: str) -> None:
    """Best-effort teardown of everything keyed to ``run_id``. Each failure is
    swallowed (e.g. no tmux installed) so the caller still returns."""
    for module, fn in _TEARDOWN:
        try:
            getattr(importlib.import_module(module), fn)(run_id)
        except Exception as exc:  # noqa: BLE001 — best-effort cleanup
            log.debug("%s.%s failed: %s", module.rsplit(".", 1)[-1], fn, exc)


__all__ = ["key_stateful_tools", "destroy_run_resources"]
