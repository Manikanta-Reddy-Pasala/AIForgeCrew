"""Logs and evidence older than the retention window are cleaned up.

Every chat leaves logs and evidence behind, and nothing removed them:
- the role logs (``logs/orchestrator-<role>.ndjson``),
- per-ticket run logs (``runs/``),
- chat traces and trajectories,
- the scheduler / health logs,
- and, in ``chat.db``, every turn's task evidence and the full output of every
  tool call.

On a busy box they filled the disk. This sweep runs at boot and then every
six hours (``runtime/periodic``). Everything older than ``log_retention_days``
(default 15; 0 turns the sweep off) is handled as follows:

* **Files** under the log folders whose last change is older than the window
  are deleted. Files that are still being written (a role log, the scheduler
  log) are trimmed in place instead:
  - an NDJSON line is dropped when its ``ts`` is older than the window;
  - a file still over ``AIFORGE_LOG_MAX_MB`` (default 50) keeps only its newest
    half.
* **Chat messages** older than the window keep their text, but their steps lose
  the bulk: the evidence of each task and of the turn, and tool output beyond a
  short head. The answer and the list of what was done stay readable.

Never touched: the user's projects (a log a job wrote in a repository is the
user's file), memory, the chats themselves, the working transcripts. Soft-fail
everywhere: a sweep that cannot read or write a file skips it.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("aiforge.log_retention")

DEFAULT_DAYS = 15
#: Folders (under the config dir) whose old files are deleted.
_DIRS = ("logs", "runs", "chat_traces", "trajectories")
#: Live append-only files at the top of the config dir that are trimmed.
_TOP_SUFFIXES = (".log", ".ndjson")
#: A kept tool output keeps this much text per field.
_KEEP_CHARS = 2000
_BULKY = ("stdout", "stderr", "output", "new_output", "log_tail", "content",
          "text", "diff", "body")
_SWEEP_EVERY_S = 6 * 3600


def days() -> int:
    """The retention window in days (0 = keep everything)."""
    try:
        from aiforge_core.config import runtime_settings
        return max(0, int(runtime_settings.get("log_retention_days")))
    except Exception:  # noqa: BLE001
        try:
            return max(0, int(os.environ.get("AIFORGE_LOG_RETENTION_DAYS",
                                             DEFAULT_DAYS)))
        except ValueError:
            return DEFAULT_DAYS


def _max_bytes() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_LOG_MAX_MB", "50"))) * 1024 * 1024
    except ValueError:
        return 50 * 1024 * 1024


def _root() -> Path:
    from aiforge_core.config.paths import config_dir
    return Path(os.path.expanduser(str(config_dir())))


# ── files ──────────────────────────────────────────────────────────────────

def _ts_of(line: str) -> "float | None":
    """The ``ts`` (ISO or epoch) of an NDJSON line, or None."""
    if not line.startswith("{"):
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    ts = rec.get("ts") if isinstance(rec, dict) else None
    if isinstance(ts, (int, float)):
        return float(ts) / (1000.0 if ts > 1e12 else 1.0)
    if isinstance(ts, str):
        try:
            from datetime import datetime
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _trim_live(path: Path, cutoff: float, cap: int) -> int:
    """Trim a file that is still written to, in place (same inode, so an open
    append handle keeps writing to it). Returns the bytes freed."""
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size == 0:
        return 0
    keep: "bytes | None" = None
    if path.suffix == ".ndjson":
        try:
            with open(path, "rb") as fh:
                first = fh.readline().decode("utf-8", "replace")
            ts = _ts_of(first)
            if ts is not None and ts < cutoff:
                kept = []
                with open(path, "rb") as fh:
                    for raw in fh:
                        t = _ts_of(raw.decode("utf-8", "replace"))
                        if t is None or t >= cutoff:
                            kept.append(raw)
                keep = b"".join(kept)
        except OSError:
            return 0
    if keep is None and size <= cap:
        return 0
    if keep is None or len(keep) > cap:
        try:
            with open(path, "rb") as fh:
                data = keep if keep is not None else fh.read()
        except OSError:
            return 0
        tail = data[-(cap // 2):]
        nl = tail.find(b"\n")
        keep = tail[nl + 1:] if 0 <= nl < len(tail) - 1 else tail
    try:
        with open(path, "r+b") as fh:
            fh.seek(0)
            fh.write(keep)
            fh.truncate()
    except OSError:
        return 0
    return max(0, size - len(keep))


def sweep_files(cutoff: float, root: "Path | None" = None) -> dict:
    """Delete old files in the log folders; trim the live ones."""
    root = root or _root()
    cap = _max_bytes()
    out = {"files_deleted": 0, "files_trimmed": 0, "bytes_freed": 0}
    recent = time.time() - 86400          # written today: still in use
    for name in _DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, topdown=False):
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    st = p.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    try:
                        p.unlink()
                        out["files_deleted"] += 1
                        out["bytes_freed"] += st.st_size
                    except OSError:
                        pass
                elif st.st_mtime >= recent and p.suffix in _TOP_SUFFIXES:
                    freed = _trim_live(p, cutoff, cap)
                    if freed:
                        out["files_trimmed"] += 1
                        out["bytes_freed"] += freed
            if Path(dirpath) != base:
                try:
                    os.rmdir(dirpath)       # only when empty
                except OSError:
                    pass
    try:
        top = [p for p in root.iterdir()
               if p.is_file() and p.suffix in _TOP_SUFFIXES]
    except OSError:
        top = []
    for p in top:
        freed = _trim_live(p, cutoff, cap)
        if freed:
            out["files_trimmed"] += 1
            out["bytes_freed"] += freed
    return out


# ── chat.db: evidence and tool output of old turns ─────────────────────────

def _cut(value, n: int = _KEEP_CHARS):
    if isinstance(value, str) and len(value) > n:
        return value[:n] + f"\n…(cut by the {days()}-day log cleanup)"
    return value


def _slim_step(step):
    """``(step, changed)``: the step without evidence and bulky output.
    None for a step that is only evidence."""
    if not isinstance(step, dict):
        return step, False
    kind = step.get("type")
    if kind == "turn_evidence":
        return None, True
    changed = False
    if kind == "subtasks" and isinstance(step.get("items"), list):
        items = []
        for it in step["items"]:
            if isinstance(it, dict) and "evidence" in it:
                it = {k: v for k, v in it.items() if k != "evidence"}
                changed = True
            items.append(it)
        if changed:
            step = {**step, "items": items}
    if "evidence" in step:
        step = {k: v for k, v in step.items() if k != "evidence"}
        changed = True
    if kind == "tool" and isinstance(step.get("result"), dict):
        res = dict(step["result"])
        for k in _BULKY:
            v = res.get(k)
            cut = _cut(v)
            if cut is not v:
                res[k] = cut
                changed = True
        if changed:
            step = {**step, "result": res}
    return step, changed


def _db_path() -> str:
    from aiforge_core.runtime.chat_store._api import _backend
    return _backend()._db_path()


def sweep_chat_db(cutoff: float, path: "str | None" = None) -> dict:
    """Strip evidence and bulky tool output from messages older than
    ``cutoff``. The text of every message is kept."""
    out = {"messages_slimmed": 0, "bytes_freed": 0}
    path = path or _db_path()
    if not path or not os.path.exists(path):
        return out
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff))
    con = sqlite3.connect(path, timeout=30.0)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        last = 0
        while True:
            rows = con.execute(
                "SELECT id, steps FROM chat_messages WHERE created_at < ? AND id > ? "
                "AND length(steps) > 2 ORDER BY id LIMIT 200", (stamp, last)).fetchall()
            if not rows:
                break
            updates = []
            for mid, raw in rows:
                last = mid
                try:
                    steps = json.loads(raw or "[]")
                except ValueError:
                    continue
                if not isinstance(steps, list):
                    continue
                new, changed = [], False
                for s in steps:
                    s2, ch = _slim_step(s)
                    changed = changed or ch
                    if s2 is not None:
                        new.append(s2)
                if changed:
                    body = json.dumps(new)
                    updates.append((body, mid))
                    out["bytes_freed"] += max(0, len(raw) - len(body))
            if updates:
                with con:
                    con.executemany("UPDATE chat_messages SET steps = ? WHERE id = ?",
                                    updates)
                out["messages_slimmed"] += len(updates)
        if out["bytes_freed"] > 20 * 1024 * 1024:
            try:
                con.execute("VACUUM")       # give the space back to the disk
            except sqlite3.OperationalError:
                pass
    finally:
        con.close()
    return out


# ── the sweep ──────────────────────────────────────────────────────────────

def sweep() -> dict:
    """One cleanup pass. Returns what it did (empty when the window is 0)."""
    n = days()
    if n <= 0:
        return {}
    cutoff = time.time() - n * 86400
    report: dict = {"days": n}
    try:
        report.update(sweep_files(cutoff))
    except Exception as exc:  # noqa: BLE001
        log.warning("log cleanup (files) failed: %s", exc)
    try:
        db = sweep_chat_db(cutoff)
        report["messages_slimmed"] = db["messages_slimmed"]
        report["bytes_freed"] = report.get("bytes_freed", 0) + db["bytes_freed"]
    except Exception as exc:  # noqa: BLE001
        log.warning("log cleanup (chat evidence) failed: %s", exc)
    if report.get("bytes_freed") or report.get("files_deleted"):
        log.info("log cleanup (%d days): %s", n, report)
    return report


def register() -> None:
    """Run once now (in the background) and every six hours."""
    from aiforge_core.runtime import periodic
    from aiforge_core.runtime.background import spawn
    spawn(sweep, name="aiforge-log-cleanup")
    periodic.register("log-cleanup", sweep, every_s=_SWEEP_EVERY_S)


__all__ = ["DEFAULT_DAYS", "days", "register", "sweep", "sweep_chat_db",
           "sweep_files"]
