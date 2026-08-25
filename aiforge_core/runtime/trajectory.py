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
from aiforge_core.config.paths import config_dir


def _trajectory_dir() -> Path:
    raw = os.environ.get("AIFORGE_TRAJECTORY_DIR")
    if raw:
        base = Path(raw).expanduser()
    else:
        # Same config dir as the rest of the app (AIFORGE_CONFIG_DIR) — a raw
        # Path.home() diverges from the operator's configured dir on
        # docker/hybrid, hiding the dumps.
        cfg = os.path.expanduser(
            str(config_dir()))
        base = Path(cfg) / "trajectories"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _safe_jsonable(val: Any) -> Any:
    """Coerce ``val`` into something ``json.dumps`` will accept."""
    try:
        json.dumps(val)
        return val
    except TypeError:
        try:
            return val.model_dump(mode="json")  # pydantic v2
        except Exception:
            pass
        try:
            return val.dict()  # pydantic v1 / dataclass-ish
        except Exception:
            pass
        return str(val)


def _part_to_dict(part: Any) -> dict[str, Any]:
    """Extract the meaningful slot from a ``google.genai.types.Part``."""
    out: dict[str, Any] = {}
    text = getattr(part, "text", None)
    if text:
        out["text"] = text
    fc = getattr(part, "function_call", None)
    if fc is not None:
        out["function_call"] = {
            "name": getattr(fc, "name", None),
            "args": _safe_jsonable(getattr(fc, "args", None)),
        }
    fr = getattr(part, "function_response", None)
    if fr is not None:
        out["function_response"] = {
            "name": getattr(fr, "name", None),
            "response": _safe_jsonable(getattr(fr, "response", None)),
        }
    return out


def _event_content(event: Any, out: dict) -> None:
    """Copy an ADK Event's content role + non-empty parts into ``out``."""
    content = getattr(event, "content", None)
    if content is None:
        return
    role = getattr(content, "role", None)
    if role is not None:
        out["role"] = role
    part_dicts = [pd for pd in
                  (_part_to_dict(p) for p in (getattr(content, "parts", None) or []))
                  if pd]
    if part_dicts:
        out["parts"] = part_dicts


def _event_actions(event: Any, out: dict) -> None:
    """Copy an ADK Event's state/artifact deltas, transfer and escalate flags."""
    actions = getattr(event, "actions", None)
    if actions is None:
        return
    for attr in ("state_delta", "artifact_delta"):
        val = getattr(actions, attr, None)
        if val:
            out[attr] = _safe_jsonable(val)
    transfer = getattr(actions, "transfer_to_agent", None)
    if transfer:
        out["transfer_to_agent"] = transfer
    if getattr(actions, "escalate", False):
        out["escalate"] = True


def _copy_attrs(event: Any, attrs, out: dict, *, skip_existing: bool = False,
                jsonable: bool = True) -> None:
    """Copy each present (not-None) attribute of ``event`` into ``out``."""
    for attr in attrs:
        val = getattr(event, attr, None)
        if val is None or (skip_existing and attr in out):
            continue
        out[attr] = _safe_jsonable(val) if jsonable else val


def _event_to_dict(event: Any) -> dict[str, Any]:
    """Coerce an ADK ``Event`` (google.adk.events.Event) into a plain
    dict for JSON storage. Handles both ADK Events and bare dicts; falls
    back to ``model_dump`` for anything else.

    Old version pulled ``type/role/text/kind`` which the ADK Event
    object never exposes at the top level — content lives on
    ``event.content.parts[i].text`` (or ``.function_call`` /
    ``.function_response``) and the speaking agent is ``event.author``.
    Result: every dumped event was ``{}``.
    """
    if isinstance(event, dict):
        return event

    out: dict[str, Any] = {}
    _copy_attrs(event, ("id", "invocation_id", "author", "timestamp",
                        "partial", "branch", "long_running_tool_ids"), out)
    _event_content(event, out)
    _event_actions(event, out)
    _copy_attrs(event, ("error_code", "error_message"), out, jsonable=False)
    # Legacy-shape fallback: ad-hoc test fixtures and older callsites pass plain
    # objects with type/tool_name/tool_args/etc. attributes. Preserve them so
    # older traces and tests stay readable.
    _copy_attrs(event, ("type", "kind", "text", "agent_name", "tool_name",
                        "tool_args", "tool_result"), out, skip_existing=True)

    if not out:
        try:
            return event.model_dump(mode="json")
        except Exception:
            return {"repr": repr(event)[:500]}
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


__all__ = [
    "dump_trajectory", "load_trajectory", "list_trajectories",
]
