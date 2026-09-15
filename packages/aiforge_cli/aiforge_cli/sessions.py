"""Which chat belongs to which folder.

Running ``aiforge`` twice in the same project should land in the same
conversation, not open chat #47. The map is a small JSON file keyed by the
folder's BOX path (the same key the API stores as the session cwd).
"""

from __future__ import annotations

import json
from pathlib import Path


def load(path: Path) -> dict[str, int]:
    """The map, or an empty one. A corrupt file is not an error worth
    stopping for — the cost of losing it is one extra `new chat`."""
    try:
        data = json.loads(path.read_text())
        return {str(k): int(v) for k, v in data.items() if str(v).isdigit()}
    # Absent, unreadable or malformed — the cost of losing it is one new chat.
    except Exception:  # noqa: BLE001
        return {}


def remember(path: Path, boxpath: str, session_id: int) -> None:
    data = load(path)
    data[boxpath] = int(session_id)
    _write(path, data)


def forget(path: Path, session_id: int) -> None:
    """Drop every folder pointing at a session that no longer exists."""
    data = {k: v for k, v in load(path).items() if v != int(session_id)}
    _write(path, data)


def for_folder(path: Path, boxpath: str, known_ids: set[int]) -> int | None:
    """The session for this folder, if it is still one the server has.

    ``known_ids`` comes from the live session list, so a chat deleted in the
    web UI does not resurrect here as a 404 on the first message.
    """
    sid = load(path).get(boxpath)
    return sid if sid in known_ids else None


def _write(path: Path, data: dict[str, int]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(path)          # atomic: a crash mid-write keeps the old map
    except OSError:
        pass                       # a read-only home is survivable
