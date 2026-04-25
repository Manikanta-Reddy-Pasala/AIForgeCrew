"""GA (GenericAgent) compatibility layer — single import seam.

ALL AIForgeCrew code that integrates with GenericAgent imports from
THIS module, not from the upstream GA package directly. When GA ships
a new version with renamed/moved symbols, edit this one file and run
the F-suite (``scripts/evals/run_genericagent_eval.py``) to gate.

Tested-against contract:
- ``agent_runner_loop(client, system_prompt, user_input, handler,
  tools_schema, max_turns, verbose, initial_user_content) -> generator``
- ``StepOutcome(data, next_prompt, should_exit)`` dataclass-shaped
- ``LLMSession(cfg=dict) -> session``  + ``ToolClient(session)``
- ``GenericAgentHandler(parent, last_history, cwd)`` base class
- Path resolution chokepoint at ``handler._get_abs_path(p)``
- Tool-before hook at ``handler.tool_before_callback(name, args, response)``
- ``do_file_patch / do_file_write / do_code_run`` are generator methods
  that ``yield`` status strings and ``return StepOutcome(...)``
- Tools schema lives at ``<ga_dir>/assets/tools_schema.json`` as a
  list of OpenAI-function-style dicts.

If a GA upgrade breaks any of those, fix HERE and bump
``GA_COMPAT_VERSION`` so eval results can correlate against it.
"""
from __future__ import annotations

import json
import os
import sys
import subprocess
from pathlib import Path
from typing import Any

GA_COMPAT_VERSION = "1.0"

# ─── GA directory resolution ───────────────────────────────────────────
def ga_dir() -> str:
    """Return absolute path to the GenericAgent source tree.

    Resolution order: ``AIFORGE_GA_DIR`` env, NUC default, MS default,
    user-home default. Raises if none exist.
    """
    p = os.environ.get("AIFORGE_GA_DIR", "")
    if p and os.path.isdir(p):
        return p
    for cand in (
        "/home/mani/genericagent",
        "/Users/manikanta/genericagent",
        os.path.expanduser("~/genericagent"),
    ):
        if os.path.isdir(cand):
            return cand
    raise RuntimeError(
        "GenericAgent dir not found; set AIFORGE_GA_DIR to override"
    )


def ga_sha() -> str | None:
    """Best-effort current GA git SHA. Used by smoke gate + telemetry to
    correlate eval results with the GA version under test. Returns None
    if GA dir isn't a git checkout."""
    try:
        cp = subprocess.run(
            ["git", "-C", ga_dir(), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if cp.returncode == 0:
            return cp.stdout.strip() or None
    except Exception:
        pass
    return None


def ga_lock_sha() -> str | None:
    """Read the SHA pinned in ``.aiforge/ga-version.lock`` (project-
    relative). Used by the smoke gate to fail loud if the live GA
    drifts off the pin without an explicit ``ga-pin`` command."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        lock = parent / ".aiforge" / "ga-version.lock"
        if lock.exists():
            try:
                line = lock.read_text().strip().splitlines()[0]
                return line.split("#", 1)[0].strip() or None
            except Exception:
                return None
    return None


# ─── Lazy GA import — keep import-time cost off this module ────────────
_GA_IMPORTED = False


def _ensure_ga_on_path() -> None:
    global _GA_IMPORTED
    if _GA_IMPORTED:
        return
    sys.path.insert(0, ga_dir())
    _GA_IMPORTED = True


def import_ga() -> dict[str, Any]:
    """Import GA and return a dict of the names AIForge uses.

    Returns ``{agent_runner_loop, exhaust, StepOutcome, LLMSession,
    ToolClient, GenericAgentHandler}``. Raises ``RuntimeError`` with a
    precise message if any of those drifted.

    Callers that need only ONE symbol can import it directly via
    ``from aiforge_core.doer.ga_compat import import_ga; ga =
    import_ga(); Handler = ga['GenericAgentHandler']``.
    """
    _ensure_ga_on_path()
    try:
        from agent_loop import (  # type: ignore
            agent_runner_loop, exhaust, StepOutcome,
        )
        from llmcore import LLMSession, ToolClient  # type: ignore
        from ga import GenericAgentHandler  # type: ignore
    except ImportError as exc:
        raise RuntimeError(
            f"GA import failed at compat layer (compat v{GA_COMPAT_VERSION}). "
            f"Likely a GA-upstream symbol moved/renamed. "
            f"Inspected ga_dir={ga_dir()!r}, sha={ga_sha()!r}. "
            f"Underlying error: {exc}"
        ) from exc
    return {
        "agent_runner_loop": agent_runner_loop,
        "exhaust": exhaust,
        "StepOutcome": StepOutcome,
        "LLMSession": LLMSession,
        "ToolClient": ToolClient,
        "GenericAgentHandler": GenericAgentHandler,
    }


# ─── Tools schema loader ───────────────────────────────────────────────
def load_tools_schema(filter_keep: set[str] | None = None,
                      filter_drop: set[str] | None = None) -> list[dict]:
    """Read GA's ``assets/tools_schema.json`` and return the OpenAI-
    function-style list, optionally filtered.

    ``filter_keep``: only retain tools whose ``function.name`` is in this
    set. None = retain all.
    ``filter_drop``: always drop tools whose name is in this set.
    Applied after ``filter_keep``.
    """
    schema_path = Path(ga_dir()) / "assets" / "tools_schema.json"
    if not schema_path.exists():
        raise RuntimeError(
            f"tools_schema.json not found at {schema_path}. "
            f"GA layout may have changed (compat v{GA_COMPAT_VERSION})."
        )
    raw = json.loads(schema_path.read_text())
    out: list[dict] = []
    for entry in raw:
        name = (entry.get("function") or {}).get("name")
        if not isinstance(name, str):
            continue
        if filter_keep is not None and name not in filter_keep:
            continue
        if filter_drop and name in filter_drop:
            continue
        out.append(entry)
    return out


# ─── ParentShim — the one piece of GA private contract we depend on ───
class ParentShim:
    """Object passed to ``GenericAgentHandler.__init__``'s ``parent`` arg.

    GA's handler reads ``self.parent.task_dir``, ``self.parent.verbose``,
    and ``self.parent._turn_end_hooks``. We carry this explicitly here
    so a future GA upgrade that adds a required parent attribute fails
    with a clear AttributeError pointing at THIS file rather than at
    a random handler call site.
    """

    __slots__ = ("task_dir", "verbose", "_turn_end_hooks")

    def __init__(self, task_dir: str, verbose: bool = False) -> None:
        self.task_dir = task_dir
        self.verbose = verbose
        self._turn_end_hooks: dict = {}


# ─── Public list of override points (audit trail for upgrades) ─────────
# When GA upgrades, walk this list, verify each name still resolves to
# the expected callable shape, update if needed, run F-suite.
GA_OVERRIDE_POINTS = (
    "_get_abs_path",       # PRIVATE — used as ScopeGuard chokepoint
    "tool_before_callback",  # public — forbidden-tools reject hook
    "do_file_patch",        # public — counter wiring
    "do_file_write",        # public — counter wiring
    "do_code_run",          # public — mvn-result detection
    "turn_end_callback",    # public — reserved for future Neo4j mirror
)


__all__ = [
    "GA_COMPAT_VERSION",
    "GA_OVERRIDE_POINTS",
    "ParentShim",
    "ga_dir",
    "ga_lock_sha",
    "ga_sha",
    "import_ga",
    "load_tools_schema",
]
