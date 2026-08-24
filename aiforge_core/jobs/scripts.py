"""Local storage + sandboxed execution for user-created *script* jobs.

Script jobs are the deterministic counterpart to ticket jobs: instead of
minting an LLM ticket every fire, a script job runs a shell script the user
built + dry-ran + approved in the conversational job builder. Because these
scripts are USER data (per-install, not source), they live in a local folder
under the config dir — ``$AIFORGE_CONFIG_DIR/jobs`` (default ``~/.aiforge/jobs``)
— NOT in the repo. The scheduler only ever executes a path that resolves INSIDE
that folder, so a stored ``script_path`` can never point the tick loop at an
arbitrary file on disk.

The conversation + mandatory dry-run + explicit approve in the builder is the
trust gate; this module is the mechanical floor under it (safe path + timeout).
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def jobs_dir() -> str:
    """Absolute local folder holding user job scripts. Created on demand."""
    cfg = os.environ.get("AIFORGE_CONFIG_DIR", "~/.aiforge")
    path = os.path.join(os.path.expanduser(cfg), "jobs")
    os.makedirs(path, exist_ok=True)
    return path


def slugify(name: str) -> str:
    """Filesystem-safe kebab slug from a job name; never empty."""
    s = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return s or "job"


def is_within_jobs_dir(path: str) -> bool:
    """True iff ``path`` resolves to a regular location INSIDE jobs_dir — the
    exec safety invariant. Guards against ``..`` traversal / absolute escapes."""
    try:
        root = os.path.realpath(jobs_dir())
        target = os.path.realpath(os.path.expanduser(path or ""))
        return target == root or target.startswith(root + os.sep)
    except Exception:  # noqa: BLE001 — any resolution failure ⇒ not safe
        return False


def write_script(name: str, content: str) -> str:
    """Persist a job script under jobs_dir and return its absolute path.

    The filename is ``<slug>-<timestamp>.sh`` so re-finalizing an edited job
    never silently overwrites the running one. Marked executable; a shebang is
    prepended when the content lacks one so a bare-body script still runs.
    """
    if not (content or "").strip():
        raise ValueError("empty script content")
    body = content if content.startswith("#!") else "#!/usr/bin/env bash\n" + content
    if not body.endswith("\n"):
        body += "\n"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    fname = f"{slugify(name)}-{stamp}.sh"
    path = os.path.join(jobs_dir(), fname)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    # 0o700, not 0o755: this is a generated script run by the SAME user.
    # World-readable+executable let any local user read what it does and
    # execute it, and it can carry job arguments.
    os.chmod(path, 0o700)
    return path


def _timeout_s() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_JOBS_SCRIPT_TIMEOUT_S", "900")))
    except (TypeError, ValueError):
        return 900


def run_script(path: str, *, timeout_s: int | None = None) -> dict:
    """Execute a stored job script with bash under a timeout.

    Returns ``{"ok": bool, "returncode": int|None, "stdout": str, "stderr": str,
    "error": str|None}``. Refuses (ok=False) any path outside jobs_dir. Never
    raises — the scheduler must survive any script failure.
    """
    if not is_within_jobs_dir(path):
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "",
                "error": f"refused: script path outside jobs dir: {path}"}
    if not os.path.isfile(os.path.expanduser(path)):
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "",
                "error": f"script not found: {path}"}
    try:
        proc = subprocess.run(
            ["/bin/bash", os.path.expanduser(path)],
            capture_output=True, text=True, timeout=timeout_s or _timeout_s(),
            cwd=jobs_dir())
    except subprocess.TimeoutExpired:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "",
                "error": "script timed out"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "returncode": None, "stdout": "", "stderr": "",
                "error": f"exec failed: {exc}"}
    return {"ok": proc.returncode == 0, "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-4000:],
            "stderr": (proc.stderr or "")[-4000:],
            "error": None if proc.returncode == 0
            else f"script exited {proc.returncode}"}


def delete_script(path: str) -> bool:
    """Remove a stored job script from disk. Refuses (returns False, no
    raise) any path outside jobs_dir or one that's already gone — same
    safety invariant as run_script, so a bad script_path can never be used
    to delete arbitrary files."""
    if not path or not is_within_jobs_dir(path):
        return False
    try:
        os.remove(os.path.expanduser(path))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


__all__ = ["jobs_dir", "slugify", "is_within_jobs_dir", "write_script",
           "run_script", "delete_script"]
