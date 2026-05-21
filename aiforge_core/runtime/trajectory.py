"""OH-parity trajectory dump + replay (sub #15).

Serializes an ADK session's ``events`` + ``state`` snapshot to JSON so
operators can replay a Doer run for debugging without re-executing
the LLM calls. Idempotent file write under
``~/.aiforge/trajectories/<ticket_id>/<run_id>.json``.

Replay is intentionally NOT a full re-execution — it produces a
human-readable transcript via :func:`load_trajectory` so the operator
can step through what the agent did.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _trajectory_dir() -> Path:
    raw = os.environ.get("AIFORGE_TRAJECTORY_DIR")
    if raw:
        base = Path(raw).expanduser()
    else:
        base = Path.home() / ".aiforge" / "trajectories"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Coerce an ADK event into a plain dict."""
    if isinstance(event, dict):
        return event
    out: dict[str, Any] = {}
    for attr in ("type", "role", "agent_name", "text", "kind"):
        val = getattr(event, attr, None)
        if val is not None:
            out[attr] = val
    # Tool call / response shape
    for attr in ("tool_name", "tool_args", "tool_result"):
        val = getattr(event, attr, None)
        if val is not None:
            try:
                json.dumps(val)
                out[attr] = val
            except TypeError:
                out[attr] = str(val)
    return out


def dump_trajectory(
    ticket_id: str | int,
    run_id: str,
    events: list[Any],
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the trajectory JSON and return ``{ok, path, n_events}``."""
    if not run_id:
        return {"ok": False, "error": "missing_run_id"}
    ticket_dir = _trajectory_dir() / str(ticket_id)
    ticket_dir.mkdir(parents=True, exist_ok=True)
    out_path = ticket_dir / f"{run_id}.json"

    payload = {
        "schema_version": 1,
        "ticket_id": str(ticket_id),
        "run_id": run_id,
        "dumped_at": time.time(),
        "events": [_event_to_dict(e) for e in events],
        "state": state or {},
    }
    try:
        out_path.write_text(
            json.dumps(payload, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        return {"ok": False, "error": "write_failed",
                "detail": str(exc)[:200]}
    return {"ok": True, "path": str(out_path),
            "n_events": len(payload["events"])}


def load_trajectory(path: str | Path) -> dict[str, Any]:
    """Read a trajectory file and return its parsed contents."""
    p = Path(path)
    if not p.is_file():
        return {"ok": False, "error": "not_found", "path": str(path)}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": "invalid_json",
                "detail": str(exc)[:200]}
    return {"ok": True, "trajectory": data}


def list_trajectories(ticket_id: str | int | None = None) -> list[str]:
    """Return absolute paths to known trajectory files."""
    root = _trajectory_dir()
    if ticket_id is not None:
        root = root / str(ticket_id)
    if not root.exists():
        return []
    return [str(p) for p in sorted(root.rglob("*.json"))]


__all__ = ["dump_trajectory", "load_trajectory", "list_trajectories"]
