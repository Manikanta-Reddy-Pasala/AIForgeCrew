"""State backup + log rotation for AiForgeMemory.

State: ~/.aiforge/codemem.state.db (sqlite — pack hashes, file
hashes, git heads). Single point of truth for delta ingest. Loss = full
re-ingest of every repo.

Strategy:
    backup_state(): copy DB to ~/.aiforge/backups/codemem.state.<ts>.db
                    using sqlite VACUUM INTO (atomic, no lock contention).
    rotate_backups(keep=7): keep newest N, drop the rest.

Logs:
    rotate_log(path, max_bytes=10MB, keep=5): if path > max_bytes,
    rename to path.1, path.2, ..., path.<keep>; truncate live file.

CLI:
    aiforge-memory ops backup        # one-shot
    aiforge-memory ops rotate-logs   # one-shot
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

STATE_DB = Path(
    os.environ.get(
        "AIFORGE_CODEMEM_STATE_DB",
        os.path.expanduser("~/.aiforge/codemem.state.db"),
    )
)
BACKUP_DIR = Path(
    os.environ.get(
        "AIFORGE_BACKUP_DIR",
        os.path.expanduser("~/.aiforge/backups"),
    )
)


@dataclass
class BackupResult:
    backed_up: list[str] = field(default_factory=list)
    rotated_out: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def backup_state(
    *, src: Path | None = None, dest_dir: Path | None = None,
) -> BackupResult:
    """sqlite3 VACUUM INTO for an atomic, consistent snapshot. Safe to
    run while the daemon is writing — sqlite handles concurrent reads."""
    src = src or STATE_DB
    dest_dir = dest_dir or BACKUP_DIR
    out = BackupResult()
    if not src.is_file():
        out.errors.append(f"source DB missing: {src}")
        return out
    dest_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    dest = dest_dir / f"codemem.state.{ts}.db"
    try:
        conn = sqlite3.connect(str(src))
        try:
            conn.execute(f"VACUUM INTO '{dest}'")
        finally:
            conn.close()
        out.backed_up.append(str(dest))
    except Exception as exc:  # noqa: BLE001
        out.errors.append(f"vacuum: {exc}")
    return out


def rotate_backups(*, dest_dir: Path | None = None, keep: int = 7,
                   ) -> BackupResult:
    """Keep the newest `keep` backups; delete older ones."""
    dest_dir = dest_dir or BACKUP_DIR
    out = BackupResult()
    if not dest_dir.is_dir():
        return out
    backups = sorted(
        dest_dir.glob("codemem.state.*.db"),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    for old in backups[keep:]:
        try:
            old.unlink()
            out.rotated_out.append(str(old))
        except OSError as exc:
            out.errors.append(f"unlink {old}: {exc}")
    return out


# ─── Log rotation ─────────────────────────────────────────────────────

def rotate_log(
    path: str | Path, *, max_bytes: int = 10 * 1024 * 1024, keep: int = 5,
) -> bool:
    """Roll a log when it exceeds max_bytes. Returns True if rotated.

    Convention:
      path           → path.1
      path.1         → path.2
      ...
      path.{keep-1}  → path.{keep}
      path.{keep}    → deleted
    """
    p = Path(path)
    if not p.is_file():
        return False
    if p.stat().st_size <= max_bytes:
        return False
    # Drop oldest first to make room.
    for i in range(keep, 0, -1):
        src = p.with_suffix(p.suffix + f".{i}")
        dst = p.with_suffix(p.suffix + f".{i + 1}")
        if i == keep and src.exists():
            try:
                src.unlink()
            except OSError:
                pass
        elif src.exists():
            try:
                shutil.move(str(src), str(dst))
            except OSError:
                pass
    # Rename live → .1, then truncate live.
    try:
        shutil.move(str(p), str(p.with_suffix(p.suffix + ".1")))
    except OSError:
        return False
    p.touch()
    return True


def rotate_known_logs() -> dict[str, bool]:
    """Rotate the standard set of AiForgeMemory log files."""
    home = Path(os.path.expanduser("~/.aiforge"))
    targets = [
        home / "scheduler.log",
        home / "scheduler.boot.log",
        home / "hook.log",
        home / "scheduler.status.json",  # not really a log but can grow
        home / "health.cron.log",
    ]
    # plus per-repo reindex logs
    for f in (home / "logs").glob("reindex-*.log") if (home / "logs").is_dir() else []:
        targets.append(f)
    return {str(t): rotate_log(t) for t in targets}
