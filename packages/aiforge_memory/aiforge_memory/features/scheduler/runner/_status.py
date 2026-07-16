"""Status journal — RepoStatus and persistence to the status JSON."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from ._paths import STATUS_PATH


# ─── Status journal ───────────────────────────────────────────────────

@dataclass
class RepoStatus:
    name: str
    last_run: float = 0.0          # unix ts
    last_status: str = ""          # 'delta_applied'|'no_changes'|'error'|...
    last_pulled: bool = False
    last_behind: int = 0
    last_error: str = ""
    next_run: float = 0.0


def _read_status() -> dict[str, RepoStatus]:
    if not STATUS_PATH.is_file():
        return {}
    try:
        raw = json.loads(STATUS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, RepoStatus] = {}
    for name, d in (raw or {}).items():
        out[name] = RepoStatus(name=name, **{
            k: d.get(k, getattr(RepoStatus(name=name), k))
            for k in ("last_run", "last_status", "last_pulled",
                      "last_behind", "last_error", "next_run")
        })
    return out


def _write_status(d: dict[str, RepoStatus]) -> None:
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATUS_PATH.write_text(json.dumps(
        {n: asdict(s) for n, s in d.items()}, indent=2,
    ))
