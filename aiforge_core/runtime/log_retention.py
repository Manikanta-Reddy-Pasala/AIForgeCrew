"""Logs and evidence older than the retention window are cleaned up.

Every chat leaves logs and evidence behind, and nothing removed them:
- the role logs (``logs/orchestrator-<role>.ndjson``),
- per-ticket run logs (``runs/``),
- chat traces and trajectories,
- and, in ``chat.db``, every turn's task evidence and the full output of every
  tool call.

On a busy box they filled the disk. This sweep runs every six hours
(``runtime/periodic``; the first run comes soon after boot). The window is
``log_retention_days`` (default 15; 0 turns the sweep off).

Log files: what happens depends on the file.
- A trace or trajectory older than the window is deleted.
- The app's own logs (``*.log`` / ``*.ndjson`` under the log folder and
  ``runs/``) are never deleted. Their writer keeps them open in append mode,
  and a deleted file would swallow its next lines.
  - A log not written to for the whole window is emptied in place.
  - A log over ``AIFORGE_LOG_MAX_MB`` (default 50) keeps its newest half.
  - Only the tail is read, never the whole file.

Chat messages older than the window keep their text, but their steps lose the
bulk: the evidence of each task and of the turn, and tool output beyond a
short head. The answer and the list of what was done stay readable. A
high-water mark (the last message id done) means each message is slimmed
once, not every six hours.

Never touched:
- the user's projects (a log a job wrote in a repository is the user's file);
- files at the top of the config folder, which other programs write
  (``scheduler.log``, ``health.cron.log``…; ``perf.ndjson`` has its own limit);
- memory, the chats themselves, the working transcripts.

The database is not VACUUMed: that would lock it for minutes and needs free
disk equal to its size. Freed pages are reused by later writes. Soft-fail
everywhere: a sweep that cannot read or write a file skips it.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path

log = logging.getLogger("aiforge.log_retention")

DEFAULT_DAYS = 15
_LOG_SUFFIXES = (".log", ".ndjson")
#: A kept tool output keeps this much text per field.
_KEEP_CHARS = 2000
_CUT_MARK = "\n…(cut by the log cleanup)"
_BULKY = ("stdout", "stderr", "output", "new_output", "log_tail", "content",
          "text", "diff", "body")
_SWEEP_EVERY_S = 6 * 3600
_STATE = "log_retention.json"


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

def _empty_in_place(path: Path) -> int:
    """Empty a log its writer may still hold open (same inode, so an append
    handle keeps writing to it). Returns the bytes freed."""
    try:
        size = path.stat().st_size
        if size:
            with open(path, "r+b") as fh:
                fh.truncate(0)
        return size
    except OSError:
        return 0


def _keep_tail(path: Path, cap: int) -> int:
    """Keep the newest ``cap // 2`` bytes of a log, cut at a line start. Reads
    only that tail. Lines appended between the read and the write are lost:
    a few lines of a log over its cap, not data. Returns the bytes freed."""
    try:
        size = path.stat().st_size
        if size <= cap:
            return 0
        keep = cap // 2
        with open(path, "r+b") as fh:
            fh.seek(size - keep)
            tail = fh.read(keep)
            nl = tail.find(b"\n")
            if 0 <= nl < len(tail) - 1:
                tail = tail[nl + 1:]
            fh.seek(0)
            fh.write(tail)
            fh.truncate()
        return max(0, size - len(tail))
    except OSError:
        return 0


def _app_log_dirs(root: "Path | None") -> list:
    """Folders of the app's own append-mode logs (never deleted, only emptied
    or cut)."""
    if root is not None:
        return [root / "logs", root / "runs"]
    try:
        from aiforge_core.config.env import LOG_DIR
        return list(dict.fromkeys([Path(LOG_DIR), Path(LOG_DIR).parent / "runs"]))
    except Exception:  # noqa: BLE001
        return [_root() / "logs", _root() / "runs"]


def _dump_dirs(root: "Path | None") -> list:
    """Folders of write-once dumps (old files are deleted): chat traces and
    trajectories, wherever their env overrides put them."""
    if root is not None:
        return [root / "chat_traces", root / "trajectories"]
    out = []
    try:
        from aiforge_core.runtime.chat_trace import trace_dir
        out.append(trace_dir())
    except Exception:  # noqa: BLE001
        out.append(_root() / "chat_traces")
    try:
        from aiforge_core.runtime.trajectory import _trajectory_dir
        out.append(_trajectory_dir())
    except Exception:  # noqa: BLE001
        out.append(_root() / "trajectories")
    return list(dict.fromkeys(out))


def _safe_base(base: Path, root: Path) -> bool:
    """Never walk the config folder itself, the home folder or "/"."""
    try:
        b = base.resolve()
        return (base.is_dir() and b != root.resolve() and b != Path.home().resolve()
                and b != Path(b.anchor))
    except OSError:
        return False


def _ours(p: Path, base: Path, runs: bool) -> bool:
    """A log the app itself writes in append mode: a role log in the log
    folder, a run log in ``runs/``. Only these are cut in place: another
    program's log may be open without append mode, and a cut would leave a
    hole its next write fills with NUL bytes."""
    if p.parent != base or p.is_symlink():
        return False
    return p.suffix == ".ndjson" and (runs or p.name.startswith("orchestrator-"))


def _under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def sweep_files(cutoff: float, root: "Path | None" = None) -> dict:
    """Delete old dumps; empty idle app logs; cut app logs over the cap. In
    the log folder, another file idle for the whole window is deleted, but
    only when that folder is inside the config folder (a log folder pointed
    at a shared place like /var/log is never swept beyond the app's own
    files)."""
    top = root or _root()
    cap = _max_bytes()
    out = {"files_deleted": 0, "files_trimmed": 0, "bytes_freed": 0}
    for i, base in enumerate(_app_log_dirs(root)):
        if not _safe_base(base, top):
            continue
        runs = i == 1
        own_folder = _under(base, top)
        try:
            entries = list(base.iterdir())
        except OSError:
            continue
        for p in entries:
            try:
                if not p.is_file() or p.is_symlink():
                    continue
                st = p.stat()
            except OSError:
                continue
            if _ours(p, base, runs):
                freed = (_empty_in_place(p) if st.st_mtime < cutoff
                         else _keep_tail(p, cap))
                if freed:
                    out["files_trimmed"] += 1
                    out["bytes_freed"] += freed
            elif own_folder and st.st_mtime < cutoff and p.suffix in (
                    ".log", ".err", ".out", ".ndjson", ".txt"):
                try:
                    p.unlink()
                    out["files_deleted"] += 1
                    out["bytes_freed"] += st.st_size
                except OSError:
                    pass
    for base in _dump_dirs(root):
        if not _safe_base(base, top):
            continue
        for dirpath, _dirs, filenames in os.walk(base, topdown=False):
            for fn in filenames:
                p = Path(dirpath) / fn
                try:
                    if p.is_symlink():
                        continue
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
            if Path(dirpath) != base:
                try:
                    os.rmdir(dirpath)       # only when empty
                except OSError:
                    pass
    return out


# ── chat.db: evidence and tool output of old turns ─────────────────────────

def _cut(value, n: int = _KEEP_CHARS):
    if (isinstance(value, str) and len(value) > n + len(_CUT_MARK)
            and not value.endswith(_CUT_MARK)):
        # Head and tail: the end of an output holds the error / the summary.
        half = n // 2
        return value[:half] + "\n…\n" + value[-half:] + _CUT_MARK
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
        cut_any = False
        for k in _BULKY:
            v = res.get(k)
            c = _cut(v)
            if c is not v:
                res[k] = c
                cut_any = True
        if cut_any:
            step = {**step, "result": res}
            changed = True
    return step, changed


def _db_path() -> str:
    from aiforge_core.runtime.chat_store._api import _backend
    return _backend()._db_path()


def _state_path(db_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(db_path)), _STATE)


def _mark(db_path: str) -> int:
    try:
        with open(_state_path(db_path), encoding="utf-8") as fh:
            return int(json.load(fh).get("chat_db_done_id") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _save_mark(db_path: str, mid: int) -> None:
    try:
        from aiforge_core.config import _atomic
        _atomic.write_text(_state_path(db_path), json.dumps({"chat_db_done_id": mid}))
    except Exception:  # noqa: BLE001
        pass


def sweep_chat_db(cutoff: float, path: "str | None" = None) -> dict:
    """Strip evidence and bulky tool output from messages older than
    ``cutoff``, each message once (from the last id done)."""
    out = {"messages_slimmed": 0, "bytes_freed": 0}
    path = path or _db_path()
    if not path or not os.path.exists(path):
        return out
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(cutoff))
    last = _mark(path)
    con = sqlite3.connect(path, timeout=30.0)
    try:
        con.execute("PRAGMA busy_timeout=30000")
        top = con.execute("SELECT COALESCE(MAX(id), 0) FROM chat_messages").fetchone()[0]
        if last > top:
            last = 0              # the database was replaced (a restore): start over
        while True:
            rows = con.execute(
                "SELECT id, steps, created_at FROM chat_messages WHERE id > ? "
                "ORDER BY id LIMIT 200", (last,)).fetchall()
            if not rows:
                break
            updates = []
            done = False
            for mid, raw, created in rows:
                if str(created or "") >= stamp:
                    done = True             # ids and times rise together
                    break
                last = mid
                try:
                    steps = json.loads(raw or "[]")
                except ValueError:
                    continue
                if not isinstance(steps, list) or not steps:
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
                with con:                    # short transactions: the server writes too
                    con.executemany("UPDATE chat_messages SET steps = ? WHERE id = ?",
                                    updates)
                out["messages_slimmed"] += len(updates)
            if done:
                break
    finally:
        con.close()
    if last != _mark(path):
        _save_mark(path, last)
    return out


# ── the sweep ──────────────────────────────────────────────────────────────

_RUN = threading.Lock()


def sweep() -> dict:
    """One cleanup pass. Returns what it did (empty when the window is 0, or
    when another pass is already running — in this process or another server
    process on the same config folder)."""
    if not _RUN.acquire(blocking=False):
        return {}
    fh = None
    try:
        try:
            import fcntl
            fh = open(_root() / "log_retention.lock", "a")
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            fh = None             # no flock on this platform: the thread lock only
        except OSError:
            if fh is not None:
                fh.close()
            return {}             # another process is sweeping
        return _sweep()
    finally:
        if fh is not None:
            fh.close()
        _RUN.release()


def _sweep() -> dict:
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
    """Every six hours; the scheduler runs a due task soon after boot.
    ``AIFORGE_LOG_CLEANUP=0`` turns the schedule off (the test suite does)."""
    if os.environ.get("AIFORGE_LOG_CLEANUP", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    from aiforge_core.runtime import periodic
    periodic.register("log-cleanup", sweep, every_s=_SWEEP_EVERY_S)
    # A first pass ten minutes after boot (the server settled), whatever the
    # scheduler's own clock says; a pass already running makes it a no-op.
    t = threading.Timer(600, sweep)
    t.daemon = True
    t.name = "aiforge-log-cleanup-first"
    t.start()


__all__ = ["DEFAULT_DAYS", "days", "register", "sweep", "sweep_chat_db",
           "sweep_files"]
