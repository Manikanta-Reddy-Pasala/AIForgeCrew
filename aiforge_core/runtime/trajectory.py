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

    for attr in ("id", "invocation_id", "author", "timestamp",
                 "partial", "branch", "long_running_tool_ids"):
        val = getattr(event, attr, None)
        if val is not None:
            out[attr] = _safe_jsonable(val)

    content = getattr(event, "content", None)
    if content is not None:
        role = getattr(content, "role", None)
        if role is not None:
            out["role"] = role
        parts = getattr(content, "parts", None) or []
        part_dicts = [_part_to_dict(p) for p in parts]
        part_dicts = [pd for pd in part_dicts if pd]
        if part_dicts:
            out["parts"] = part_dicts

    actions = getattr(event, "actions", None)
    if actions is not None:
        state_delta = getattr(actions, "state_delta", None)
        if state_delta:
            out["state_delta"] = _safe_jsonable(state_delta)
        artifact_delta = getattr(actions, "artifact_delta", None)
        if artifact_delta:
            out["artifact_delta"] = _safe_jsonable(artifact_delta)
        transfer = getattr(actions, "transfer_to_agent", None)
        if transfer:
            out["transfer_to_agent"] = transfer
        if getattr(actions, "escalate", False):
            out["escalate"] = True

    error_code = getattr(event, "error_code", None)
    if error_code is not None:
        out["error_code"] = error_code
    error_msg = getattr(event, "error_message", None)
    if error_msg is not None:
        out["error_message"] = error_msg

    # Legacy-shape fallback: ad-hoc test fixtures and older callsites
    # pass plain objects with type/tool_name/tool_args/etc. attributes.
    # Preserve them so older traces and tests stay readable.
    for attr in ("type", "kind", "text", "agent_name",
                 "tool_name", "tool_args", "tool_result"):
        val = getattr(event, attr, None)
        if val is not None and attr not in out:
            out[attr] = _safe_jsonable(val)

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


def _summarize_trajectory(events: list[dict]) -> str:
    """Render a short trajectory summary suitable for AFM Note_v2 body.

    Picks the most signal-dense slots per event (author, tool calls,
    final text snippet) so a future ticket asking "have we seen this
    before?" can rerank trajectories by content instead of just by
    ticket id."""
    lines: list[str] = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        author = ev.get("author") or ev.get("agent_name") or ev.get("role")
        snippet_parts: list[str] = []
        for part in ev.get("parts") or []:
            if not isinstance(part, dict):
                continue
            if part.get("function_call"):
                fc = part["function_call"]
                snippet_parts.append(
                    f"call {fc.get('name')}({_short(fc.get('args'))})"
                )
            elif part.get("function_response"):
                fr = part["function_response"]
                snippet_parts.append(
                    f"resp {fr.get('name')} → {_short(fr.get('response'))}"
                )
            elif part.get("text"):
                snippet_parts.append(_short(part["text"]))
        if not snippet_parts:
            text = ev.get("text") or ev.get("tool_name") or ""
            if text:
                snippet_parts.append(_short(text))
        if not snippet_parts:
            continue
        joined = " | ".join(snippet_parts)[:240]
        lines.append(f"{author or '?'}: {joined}")
    return "\n".join(lines[:80])


def _short(val: Any, limit: int = 120) -> str:
    s = str(val) if val is not None else ""
    s = s.replace("\n", " ").strip()
    if len(s) > limit:
        s = s[: limit - 1] + "…"
    return s


def index_trajectory_to_memory(
    *,
    trajectory_path: str | Path,
    repo: str,
    ticket_identifier: str,
    title_prefix: str = "trajectory",
) -> dict:
    """Persist a one-line-per-event trajectory summary into AFM as a
    queryable ``Note_v2`` so future tickets can rerank "have we run
    something like this before?" against past runs.

    The note body holds the rendered summary (capped to ~80 lines); a
    tag points back at the JSON file on disk for full replay.

    Soft-fails on any error — never raises. Returns
    ``{ok, note_id?, error?}``.
    """
    out: dict[str, Any] = {"ok": False}
    if os.environ.get("AIFORGE_TRAJECTORY_INDEX", "1") in ("0", "false", ""):
        out["error"] = "disabled"
        return out
    try:
        loaded = load_trajectory(trajectory_path)
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"load_failed: {exc}"
        return out
    if not loaded.get("ok"):
        out["error"] = loaded.get("error", "load_failed")
        return out

    traj = loaded["trajectory"]
    body = _summarize_trajectory(traj.get("events") or [])
    if not body.strip():
        out["error"] = "empty_summary"
        return out

    try:
        from aiforge_memory.features.memory.store import upsert_note
    except ImportError:
        try:
            from aiforge_memory.memory.store import upsert_note  # type: ignore
        except ImportError:
            out["error"] = "aiforge_memory_not_installed"
            return out

    try:
        from neo4j import GraphDatabase
    except ImportError:
        out["error"] = "neo4j_driver_missing"
        return out

    uri = os.environ.get("AIFORGE_NEO4J_URI", "bolt://127.0.0.1:7687")
    user = os.environ.get("AIFORGE_NEO4J_USER", "neo4j")
    pw = os.environ.get(
        "AIFORGE_NEO4J_PASSWORD",
        os.environ.get("NEO4J_PASSWORD", "password"),
    )
    try:
        drv = GraphDatabase.driver(uri, auth=(user, pw))
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"neo4j_connect_failed: {exc}"
        return out

    try:
        result = upsert_note(
            drv, repo=repo,
            title=f"{title_prefix}:{ticket_identifier}:{traj.get('run_id', '')}",
            body=body,
            author="trajectory_indexer",
            tags=[
                f"ticket:{ticket_identifier}",
                f"trajectory_path:{str(trajectory_path)}",
                "kind:trajectory",
            ],
        )
        out["ok"] = True
        out["note_id"] = result.get("id")
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"upsert_failed: {exc}"
    finally:
        try:
            drv.close()
        except Exception:
            pass
    return out


__all__ = [
    "dump_trajectory", "load_trajectory", "list_trajectories",
    "index_trajectory_to_memory",
]
