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
from ._shell_wait import (  # noqa: F401  # re-exported
    _await_exit,
    _collect_output,
    _drain,
    _kill_proc,
    _timeout_result,
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


def _run_refusal(cmd: str, args: dict, base: str, *,
                 background: bool = False) -> dict | None:
    """The pre-flight gates, all fail-CLOSED. None means the command may run.

    - a destructive delete without an explicit confirm;
    - blanket git staging, which in chat would sweep the user's UNRELATED files
      (and the agent's own artifacts) into a commit. We do NOT execute it — the
      soft error makes the agent loop re-issue a targeted `git add <paths>`;
    - a FOREGROUND server-start: it never returns, so run_command would poll it
      forever (no wall clock) and wedge the turn — the chat "network error"
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
    if _is_server_start(cmd, base) and not background:
        return {"ok": False, "blocked": "server_start",
                "error": _SERVER_START_REFUSAL}
    missing = _preflight_missing_path(cmd, base)
    if missing:
        return {"ok": False, "blocked": "missing_path", "error": missing}
    return None


def _marked_background(args: dict, cmd: str) -> bool:
    """True when the user or the model marked this command as background.

    ``background: true``, or a command that ends in a trailing ``&`` (not
    ``&&``). That process must keep running after this tool returns."""
    flag = args.get("background")
    if flag in (True, 1, "1", "true", "True", "yes"):
        return True
    s = (cmd or "").rstrip()
    return len(s) >= 2 and s.endswith("&") and not s.endswith("&&") \
        and s[-2] != "&"


def _without_trailing_amp(cmd: str) -> str:
    """Run a trailing ``&`` in the foreground of its own session.

    ``sleep 30 &`` makes the shell exit at once and leaves the child with
    no handle for Stop. Dropping that one ``&`` (not ``&&``) keeps the
    command as the session leader, so Stop still reaches it."""
    s = (cmd or "").rstrip()
    if len(s) >= 2 and s.endswith("&") and not s.endswith("&&") and s[-2] != "&":
        return s[:-1].rstrip()
    return cmd


def _start_background(cmd: str, base: str) -> dict:
    """Spawn ``cmd`` and return a handle. Do not kill it on the way out."""
    from aiforge_core.runtime import chat_cancel
    cmd = _without_trailing_amp(cmd)
    spool = Spool()
    try:
        proc = subprocess.Popen(
            cmd, shell=True, cwd=base,
            stdout=spool.out, stderr=spool.err,
            start_new_session=True)
    except Exception as exc:  # noqa: BLE001
        spool.close()
        return {"ok": False, "error": str(exc)}
    spool.pgid = proc.pid
    try:
        sid = chat_cancel.active()
    except Exception:  # noqa: BLE001
        sid = None
    from aiforge_core.runtime import cmd_jobs
    job = cmd_jobs.adopt_spooled(proc, spool, cmd, base, explicit=True,
                                 session_id=sid)
    out = dict(getattr(job, "handle", {}) or {})
    out["id"] = job.key
    out["note"] = (out.get("note", "") + f" Check on it any time with "
                   f"command_output(id='{job.key}') or command_wait(id="
                   f"'{job.key}'); command_kill stops it.").strip()
    return out


def _t_run_command(args: dict, cwd: str) -> dict:
    cmd = args["cmd"]
    root = _workspace_root()
    base = str(root) if root is not None else cwd
    background = _marked_background(args, cmd)
    refusal = _run_refusal(cmd, args, base, background=background)
    if refusal is not None:
        return refusal
    if background:
        return _start_background(cmd, base)
    # No wall clock by default: a build that keeps printing (npm ci, mvn
    # package) runs until it is done. It is stopped when it goes silent — no
    # output, no CPU — for AIFORGE_CMD_IDLE_S, or at the wall clock the model
    # (``timeout``) or the operator (AIFORGE_CHAT_CMD_TIMEOUT_S) asked for.
    from aiforge_core.runtime.cmd_idle import idle_limit_s, wall_cap_s
    timeout = wall_cap_s(args.get("timeout"), "AIFORGE_CHAT_CMD_TIMEOUT_S")
    idle_s = idle_limit_s()
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
    import time as _time
    started = _time.monotonic()
    handed = False
    try:
        res = _run_to_end(proc, timeout, spool, idle_s)
        if res.get("_handoff"):
            handed = True
            return _hand_off(proc, spool, cmd, base, res["_handoff"],
                             started, timeout, idle_s)
        return res
    finally:
        if not handed:
            spool.release_children()
            spool.close()


def _hand_off(proc, spool, cmd, base, why, started, timeout, idle_s) -> dict:
    """Still running at the check-in (or it just printed an error / a
    prompt): do not wait it out. It becomes a job this turn owns, and the
    model sees what it has printed so far and decides."""
    from aiforge_core.runtime import chat_cancel, cmd_jobs
    try:
        sid = chat_cancel.active()
    except Exception:  # noqa: BLE001
        sid = None
    job = cmd_jobs.adopt_spooled(
        proc, spool, cmd, base, explicit=False, session_id=sid, idle_s=idle_s,
        deadline=(started + timeout) if timeout else None, started=started)
    return cmd_jobs.look(job, why)


def _run_to_end(proc, timeout: float, spool, idle_s: float = 0.0) -> dict:
    from aiforge_core.runtime import chat_cancel
    sid = chat_cancel.active()
    if sid is not None:
        try:
            chat_cancel.track_pgid(sid, os.getpgid(proc.pid))
        except Exception:  # noqa: BLE001
            pass
    from aiforge_core.runtime.cmd_jobs import checkin_s
    stopped = _await_exit(proc, timeout, sid, spool, idle_s, checkin_s())
    if stopped is not None:
        return stopped
    out, err = _collect_output(proc, spool)
    return {"ok": proc.returncode == 0, "code": proc.returncode,
            "stdout": out[-_MAX_OBS:], "stderr": err[-_MAX_OBS:]}
