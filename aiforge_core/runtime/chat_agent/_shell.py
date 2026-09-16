from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from ._cmd_guard import (  # noqa: F401  # re-exported
    _BASH,
    _BLANKET_ADD_SELECTORS,
    _ENV_ASSIGN_RE,
    _GIT_GLOBAL_VALUE_OPTS,
    _NODE_PMS,
    _NODE_SERVER_SUBS,
    _SCRIPT_EXTS,
    _SCRIPT_RUNNERS,
    _SEGMENT_SPLIT_RE,
    _SERVER_BY_PROG,
    _SERVER_PREFIX_WORDS,
    _SERVER_PROGRAMS,
    _SERVER_PY_MODULES,
    _cmd_starts_server,
    _git_subcommand,
    _is_autostage_commit,
    _is_blanket_add,
    _is_blanket_git,
    _is_literal_path,
    _is_server_start,
    _mask_noncode,
    _node_pm_starts_server,
    _NonCodeMasker,
    _peel_benign_prefixes,
    _peel_prefixes,
    _preflight_missing_path,
    _python_starts_server,
    _resolved,
    _script_arg,
    _script_missing_error,
    _script_starts_server,
    _script_token,
    _segment_starts_server,
    _segment_tokens,
)
from ._obs_trim import (  # noqa: F401  # re-exported
    _OBS_TEXT_KEYS,
    _cut_at_structure,
    _largest_text_key,
    _smart_truncate_obs,
    _trimmed_json,
)
from ._spool import Spool

# Digits too: a tool name like `s3_get` was cut to `s`. See _prompt._credible_action
# for why a match alone is not yet a tool call.
_ACTION_RE = re.compile(r"ACTION:\s*([a-z_]\w*)", re.IGNORECASE | re.ASCII)
_ARGS_RE = re.compile(r"ARGS_JSON:\s*(\{.*\})", re.IGNORECASE | re.DOTALL)
_FINAL_RE = re.compile(r"FINAL:\s*(.*)", re.IGNORECASE | re.DOTALL)
_ASK_RE = re.compile(r"ASK:\s*(.*)", re.IGNORECASE | re.DOTALL)
# Everything after ``THOUGHT:`` up to the next ``\nMARKER:`` line, or the end.
# Written as a tempered greedy token rather than ``(.*?)(?:\n[A-Z_]+:|$)``:
# a reluctant quantifier whose follower can match the empty string (``$``
# is zero-width) is the shape S6019 flags, and it read as if stopping were
# optional. This says the same thing in one pass — consume anything that is
# not the start of the next marker line. ``[ \t]*`` after the colon, NOT
# ``\s*``: eating the newline would let the token consume the very marker
# line that is supposed to stop it.
_THOUGHT_RE = re.compile(r"THOUGHT:[ \t]*((?:(?!\n[A-Z_]+:)[\s\S])*)",
                         re.IGNORECASE)

_MAX_OBS = 6000  # truncate tool output fed back to the model
# Content-READ tools return a document the model must see IN FULL to work with
# (a long Confluence page, a Jira issue, a file). The generic 6k cap truncated
# them mid-page — the model then reported "API truncation" and gave up. Give
# these a much larger observation budget. Tunable via env.
try:
    _MAX_OBS_READ = max(_MAX_OBS, int(os.environ.get("AIFORGE_CHAT_MAX_OBS_READ",
                                                     "80000")))
except (TypeError, ValueError):
    _MAX_OBS_READ = 80000
_READ_OBS_TOOLS = frozenset({
    "confluence_read", "confluence_spaces", "confluence_page_by_title",
    "confluence_labels", "confluence_comments", "confluence_descendants",
    "jira_read", "jira_worklog", "jira_projects", "jira_remote_links",
    "context_gather", "resolve_repo", "jira_resolve_project",
    "confluence_resolve_space",
    "jira_boards", "jira_sprints", "jira_sprint_issues", "jira_dashboards",
    "jira_dashboard_read", "jira_myself", "file_read", "read_files", "read_lines",
    "gitlab_read", "web_fetch", "web_crawl", "email_read",
    # A CI pipeline's answer is a job log tail. Under the blunt 6k cap the
    # `jobs` array alone pushed it out of the observation — at 14 jobs,
    # measured — so the model read truncated JSON and no failure reason.
    "gitlab_pipeline", "gitlab_pipelines", "gitlab_pipeline_watch",
})


# ─── Chat-side commit hygiene: REFUSE blanket git stages ─────────
#
# The chat agent runs in the user's (possibly dirty) repo and writes files via
# BOTH the file tools AND the shell. A blanket ``git add -A`` / ``git add .`` /
# ``git commit -a`` issued by the model would sweep the user's UNRELATED edits
# (and the agent's own artifacts) into a commit. Rather than try to REWRITE the
# command to stage only the agent's files — fragile, the source of repeated
# edge-case bugs — we simply REFUSE the blanket stage (fail-CLOSED) and tell the
# model to stage specific paths. The system prompt already instructs this, and
# the agent loop turns the refusal into an observation so the model re-issues a
# targeted ``git add <paths>`` (which runs normally).


def _workspace_root() -> Path | None:
    from aiforge_core.runtime import request_context
    raw = request_context.get_workspace_dir()
    return Path(os.path.expanduser(raw)).resolve() if raw else None


def _resolve(cwd: str, path: str) -> Path:
    """Resolve ``path`` against the session cwd. When AIFORGE_WORKSPACE_DIR
    is set, reject anything that escapes it; otherwise total freedom."""
    base = Path(cwd).expanduser().resolve()
    p = (base / path).resolve() if not os.path.isabs(path) else Path(path).resolve()
    root = _workspace_root()
    if root is not None and root not in p.parents and p != root:
        raise PermissionError(f"path escapes AIFORGE_WORKSPACE_DIR: {path}")
    return p


# ─────────────────────────── tools ──────────────────────────────────

def _t_file_read(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args["path"])
    # A directory passed to file_read used to just error ("not a file"), which
    # reads as nonsense when the model is exploring. Return its LISTING instead
    # (same shape as list_dir) so the mistake is still useful navigation.
    if p.is_dir():
        try:
            entries = [(c.name + "/") if c.is_dir() else c.name
                       for c in sorted(p.iterdir())]
        except OSError as exc:
            return {"ok": False, "error": f"cannot list dir {args['path']}: {exc}"}
        return {"ok": True, "is_dir": True, "path": str(p), "entries": entries,
                "note": "this is a DIRECTORY — listing its entries; "
                        "call file_read on a specific file to read contents"}
    if not p.is_file():
        return {"ok": False, "error": f"no such file or directory: {args['path']}"}
    return {"ok": True, "content": p.read_text(encoding="utf-8", errors="replace")}


def _requested_paths(args: dict) -> list[str]:
    """``paths`` as a list — it also arrives as a comma/newline string, or under
    the singular ``path`` key."""
    raw = args.get("paths")
    if raw is None:
        raw = args.get("path") or []
    if isinstance(raw, str):
        raw = [s for s in re.split(r"[,\n]+", raw) if s.strip()]
    return [str(p).strip() for p in (raw or []) if str(p).strip()]


def _read_files_cap() -> int:
    try:
        return max(200, int(os.environ.get(
            "AIFORGE_CHAT_READ_FILES_PER_CAP", "6000")))
    except ValueError:
        return 6000


def _one_read_block(path: str, cwd: str, per_cap: int) -> tuple[str, bool]:
    """``(=== path === block, ok)`` for one file, capped so no single file eats
    the observation budget."""
    r = _t_file_read({"path": path}, cwd)
    if not (isinstance(r, dict) and r.get("ok") and not r.get("is_dir")):
        err = r.get("error") if isinstance(r, dict) else "unknown error"
        return f"=== {path} ===\n[read failed: {err}]", False
    txt = str(r.get("content") or "")
    if len(txt) > per_cap:
        txt = (txt[:per_cap] + f"\n…[truncated {len(txt) - per_cap} chars — "
               "use read_lines for the rest]")
    return f"=== {path} ===\n{txt}", True


def _t_read_files(args: dict, cwd: str) -> dict:
    """Read MANY files in ONE call — the batched form of :func:`_t_file_read`.

    Local models on a long ONE-AT-A-TIME read chain lose track of what they've
    read and stall re-reading old files; batching a whole set into a single turn
    sidesteps that entirely. Accepts ``paths`` (a list, or a comma/newline
    string). Returns every file's content concatenated under ``=== path ===``
    headers in one ``content`` field, each file capped so no single file eats the
    observation budget (raise AIFORGE_CHAT_READ_FILES_PER_CAP; default 6000)."""
    paths = _requested_paths(args)
    if not paths:
        return {"ok": False, "error": "missing 'paths' (a list of file paths)"}
    per_cap = _read_files_cap()
    max_files = 60
    dropped = max(0, len(paths) - max_files)
    blocks = [_one_read_block(p, cwd, per_cap) for p in paths[:max_files]]
    ok_n = sum(1 for _, ok in blocks if ok)
    err_n = len(blocks) - ok_n
    note = f"{ok_n} read, {err_n} failed"
    if dropped:
        note += f", {dropped} skipped (>{max_files}-file cap — call again)"
    return {"ok": ok_n > 0, "count": ok_n, "read": ok_n, "failed": err_n,
            "content": "\n\n".join(b for b, _ in blocks), "note": note}


# Code extensions where a syntax check is meaningful (so we never reject a
# legit prose/data file for unbalanced braces). The guard is brace-balance for
# most, compile() for .py.
_SYNTAX_EXTS = (".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".kt", ".kts",
                ".go", ".rs", ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".php",
                ".rb", ".swift", ".scala")


def _syntax_check(path: str, content: str, args: dict) -> str | None:
    """Return an error string if ``content`` is broken code, else None. Only
    runs for known code extensions, skips empty files, and honours force:true."""
    if args.get("force") or not content.strip():
        return None
    if not str(path).lower().endswith(_SYNTAX_EXTS):
        return None
    try:
        from aiforge_core.runtime.syntax_guard import validate_syntax
        ok, err = validate_syntax(path, content)
        return None if ok else err
    except Exception:  # noqa: BLE001 — never let the guard break a write
        return None


def _t_file_write(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args["path"])
    content = args.get("content", "")
    bad = _syntax_check(str(p), content, args)
    if bad:
        return {"ok": False, "error": "syntax_invalid", "detail": bad,
                "hint": "fix the syntax, or pass force:true to write anyway"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return {"ok": True, "path": str(p), "bytes": len(content)}


def _t_file_patch(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args["path"])
    if not p.is_file():
        return {"ok": False, "error": "not_found"}
    body = p.read_text(encoding="utf-8")
    old = args["old_text"]
    n = body.count(old)
    if n == 0:
        return {"ok": False, "error": "old_text_not_found"}
    if n > 1:
        return {"ok": False, "error": "ambiguous_match", "occurrences": n}
    new_body = body.replace(old, args["new_text"], 1)
    bad = _syntax_check(str(p), new_body, args)
    if bad:
        return {"ok": False, "error": "syntax_invalid", "detail": bad,
                "hint": "the edit would break the file; fix it or pass force:true"}
    p.write_text(new_body, encoding="utf-8")
    return {"ok": True, "path": str(p)}


def _t_list_dir(args: dict, cwd: str) -> dict:
    p = _resolve(cwd, args.get("path", "."))
    if not p.is_dir():
        return {"ok": False, "error": f"not a dir: {args.get('path')}"}
    entries = [
        (c.name + "/") if c.is_dir() else c.name
        for c in sorted(p.iterdir())
    ]
    return {"ok": True, "entries": entries}


_BLANKET_GIT_REFUSAL = (
    "Blanket staging (git add -A / git add . / git commit -a) is disabled in "
    "chat to avoid committing unrelated files. Stage ONLY the files you "
    "changed: `git add <path1> <path2>` then `git commit -m \"...\"` then "
    "`git push`.")

_SERVER_START_REFUSAL = (
    "This starts a long-lived server/dev process that won't return, so "
    "run_command would block the whole turn. Use the `serve` tool instead — "
    "serve(cmd=\"…\") starts it in the background and gives you the URL "
    "immediately (stop it later with stop_service). If you must start it "
    "yourself, redirect its output on Linux: `cmd > app.log 2>&1 &` (a "
    "bare `&` child, and on other systems any child, is stopped when the "
    "command returns).")


def _run_refusal(cmd: str, args: dict, base: str) -> dict | None:
    """The pre-flight gates, all fail-CLOSED. None means the command may run.

    - a destructive delete without an explicit confirm;
    - blanket git staging, which in chat would sweep the user's UNRELATED files
      (and the agent's own artifacts) into a commit. We do NOT execute it — the
      soft error makes the agent loop re-issue a targeted `git add <paths>`;
    - a FOREGROUND server-start: it never returns, so run_command would poll it
      until the (10-min) timeout and wedge the turn — the chat "network error"
      bug. Redirected to `serve`, which backgrounds it and returns the URL;
    - a literal `cd <missing dir>` / `bash <missing script>`, refused with an
      actionable error instead of the shell's cryptic "No such file or
      directory" (which the model then thrashes on).
    """
    from aiforge_core.runtime.tools import delete_guard
    allow_delete = delete_guard.allow_delete(
        ("AIFORGE_CHAT_ALLOW_DELETE", "AIFORGE_ALLOW_DELETE"))
    if not allow_delete and not args.get("confirm_delete") \
            and delete_guard.is_destructive_delete(cmd, base):
        return {"ok": False, "blocked": "delete",
                "error": delete_guard.REFUSAL + " (re-issue with "
                         "confirm_delete=true after the user agrees.)"}
    if _is_blanket_git(cmd):
        return {"ok": False, "blocked": "blanket_git",
                "error": _BLANKET_GIT_REFUSAL}
    if _is_server_start(cmd, base):
        return {"ok": False, "blocked": "server_start",
                "error": _SERVER_START_REFUSAL}
    missing = _preflight_missing_path(cmd, base)
    if missing:
        return {"ok": False, "blocked": "missing_path", "error": missing}
    return None


def _drain(proc, timeout: float = 5) -> tuple[str, str] | None:
    """Whatever the process buffered, or None if it could not be collected."""
    try:
        out, err = proc.communicate(timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    return out or "", err or ""


def _timeout_result(proc, timeout: int, spool=None) -> dict:
    """Capture whatever the command buffered BEFORE we kill it, so the agent
    sees partial output (e.g. which tests ran/passed before the hang) and can
    adapt — instead of a blind "timeout" with no signal."""
    import signal as _sig

    from aiforge_core.runtime import proc_signals
    proc_signals.kill_group(proc_signals.group_of(proc), _sig.SIGTERM)
    if spool is not None:
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001 — ignored SIGTERM
            pass
        # The shell may be gone while a child that ignored SIGTERM is not.
        spool.kill_group()
        drained = spool.read()
    else:
        drained = _drain(proc)
    if drained is None:
        _kill_proc(proc)
        drained = ("", "")
    out, err = drained
    return {"ok": False, "timed_out": True, "code": None,
            "stdout": out[-_MAX_OBS:], "stderr": err[-_MAX_OBS:],
            "error": f"timed out after {timeout}s — PARTIAL output "
            "above. This is not a failure of your change: the command "
            "just ran longer than the limit. Next: run a NARROWER "
            "command (one test file or a single test case), or re-issue "
            "this exact command with a larger \"timeout\" (e.g. 600). Do "
            "NOT undo your edits over a timeout."}


def _await_exit(proc, timeout: int, sid, spool=None) -> dict | None:
    """Poll until the process exits; a dict when it was stopped or timed out."""
    import time as _time

    from aiforge_core.runtime import chat_cancel
    deadline = _time.monotonic() + timeout
    while proc.poll() is None:
        if sid is not None and chat_cancel.is_cancelled(sid):
            _kill_proc(proc)
            return {"ok": False, "stopped": True, "error": "stopped by user"}
        if _time.monotonic() > deadline:
            return _timeout_result(proc, timeout, spool)
        if spool is not None and spool.too_big():
            spool.kill_group()
            _kill_proc(proc)
            out, err = spool.read()
            return {"ok": False, "code": None, "stdout": out[-_MAX_OBS:],
                    "stderr": err[-_MAX_OBS:], "error": spool.too_big_error()}
        _time.sleep(0.2)
    return None


def _collect_output(proc, spool=None) -> tuple[str, str]:
    """Bound communicate(): a daemon grandchild inheriting the stdout pipe
    (e.g. `npm run dev &`) keeps it open after the process exits, so an
    un-timed communicate() blocks forever even past the deadline. Spooled
    output is simply read back."""
    if spool is not None:
        return spool.read()
    try:
        ct = int(os.environ.get("AIFORGE_COMMUNICATE_TIMEOUT_S", "10"))
    except (TypeError, ValueError):
        ct = 10
    try:
        out, err = proc.communicate(timeout=ct)
        return out or "", err or ""
    except subprocess.TimeoutExpired:
        _kill_proc(proc)
        return _drain(proc) or ("", "")


def _t_run_command(args: dict, cwd: str) -> dict:
    cmd = args["cmd"]
    root = _workspace_root()
    base = str(root) if root is not None else cwd
    refusal = _run_refusal(cmd, args, base)
    if refusal is not None:
        return refusal
    # Default generous so dependency installs / builds (npm ci, mvn package,
    # pip install) aren't killed mid-run; agent may override per call.
    default_to = int(os.environ.get("AIFORGE_CHAT_CMD_TIMEOUT_S", "600"))
    timeout = int(args.get("timeout", default_to))
    spool = None
    try:
        spool = Spool()
        # Its own process group, so the Stop button can kill the whole tree
        # (the shell + its children).
        proc = subprocess.Popen(
            cmd, shell=True, cwd=base,
            stdout=spool.out, stderr=spool.err,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        if spool is not None:
            spool.close()
        return {"ok": False, "error": str(exc)}
    spool.pgid = proc.pid             # start_new_session: its own group
    try:
        return _run_to_end(proc, timeout, spool)
    finally:
        spool.release_children()
        spool.close()


def _run_to_end(proc, timeout: int, spool) -> dict:
    from aiforge_core.runtime import chat_cancel
    sid = chat_cancel.active()
    if sid is not None:
        try:
            chat_cancel.track_pgid(sid, os.getpgid(proc.pid))
        except Exception:  # noqa: BLE001
            pass
    stopped = _await_exit(proc, timeout, sid, spool)
    if stopped is not None:
        return stopped
    out, err = _collect_output(proc, spool)
    return {"ok": proc.returncode == 0, "code": proc.returncode,
            "stdout": out[-_MAX_OBS:], "stderr": err[-_MAX_OBS:]}


def _kill_proc(proc) -> None:
    from aiforge_core.runtime import proc_signals
    if not proc_signals.stop_group(proc_signals.group_of(proc),
                                   pid=getattr(proc, "pid", None),
                                   pause_s=0.0):
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 — already gone
            pass
    # Reap so the killed child's pipe FDs are freed (no zombie leak).
    try:
        proc.communicate(timeout=5)
    except Exception:  # noqa: BLE001
        pass


