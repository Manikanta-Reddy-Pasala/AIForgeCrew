"""Filesystem + shell tools (file_read/write/patch, list_dir, run_shell,
grep_repo, read_lines) + the touched-path tracker and per-run read cache.

Split out of the former ``doer_tools`` module — moved verbatim.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading

from ..sandbox import resolve_inside_root, root
from ..syntax_guard import validate_syntax


def _compact_digest(stdout: str, stderr: str, returncode) -> str:
    """Soft wrapper around output_compactor.digest — never raises into a tool."""
    try:
        from ..output_compactor import digest as _d
        return _d(stdout, stderr, returncode)
    except Exception:  # noqa: BLE001
        return ""


# ─── Touched-path tracker (informational only) ─────────────────────────
#
# The Doer's file tools still record every repo-relative path they mutate
# here, but staging NO LONGER depends on it: the Doer runs in an ISOLATED
# git worktree branched from a clean base, so "everything changed in the
# worktree" == "the agent's work". ``git_commit`` and the end-of-ticket PR
# step therefore stage with ``git add -A`` (artifact pathspecs excluded),
# which also captures deletions/renames the touched-list used to drop. The
# tracker is kept as a harmless record (other tooling/tests reference it).
_TOUCHED: set[str] = set()
_TOUCHED_LOCK = threading.Lock()


def record_touch(path: str) -> None:
    """Record ``path`` (normalised repo-relative) as Doer-mutated.

    Uses the same sandbox normalisation the tools use so the recorded
    path matches what ``git`` sees. Soft: a path outside the root is
    stored best-effort rather than raising into the tool call."""
    if not path:
        return
    try:
        rel = str(resolve_inside_root(path).relative_to(root()))
    except Exception:  # noqa: BLE001
        rel = str(path).strip().lstrip("/")
    if rel and rel != ".":
        with _TOUCHED_LOCK:
            _TOUCHED.add(rel)


def touched_paths() -> list[str]:
    """Sorted list of repo-relative paths the Doer has mutated this run."""
    with _TOUCHED_LOCK:
        return sorted(_TOUCHED)


def reset_touched() -> None:
    """Clear the tracker at the start of a new ticket/run so stale paths
    from a prior run don't get staged."""
    with _TOUCHED_LOCK:
        _TOUCHED.clear()


# Per-process read cache so the doer↔refiner↔feedback loop doesn't re-read
# unchanged files every iteration. Keyed by (abspath, mtime_ns): a
# file_write/file_patch bumps mtime → auto-miss → fresh read, so a stale
# entry can never be served. Bounded FIFO.
_READ_CACHE: dict[str, tuple[int, str]] = {}
_READ_CACHE_MAX = 256


def file_read(path: str) -> dict:
    """Read a UTF-8 text file relative to the repo root.

    Returns ``{ok, path, content, bytes}`` on success, or
    ``{ok: False, error}``. Cached per (path, mtime) within the run.
    """
    try:
        p = resolve_inside_root(path)
        if not p.is_file():
            return {"ok": False, "error": f"not a file: {path}"}
        ap = str(p)
        try:
            mt = p.stat().st_mtime_ns
        except OSError:
            mt = -1
        hit = _READ_CACHE.get(ap)
        if hit is not None and hit[0] == mt:
            text = hit[1]
        else:
            text = p.read_text(encoding="utf-8", errors="replace")
            if len(_READ_CACHE) >= _READ_CACHE_MAX:
                _READ_CACHE.pop(next(iter(_READ_CACHE)))
            _READ_CACHE[ap] = (mt, text)
        return {"ok": True, "path": path,
                "content": text, "bytes": len(text.encode("utf-8"))}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def file_write(path: str, content: str) -> dict:
    """Create or overwrite a UTF-8 text file relative to the repo root.

    Runs :func:`syntax_guard.validate_syntax` first; rejects + returns
    a hint string when the draft fails so the Doer can self-correct
    on the next turn instead of leaking corrupt output to disk. Set
    ``AIFORGE_DOER_SKIP_SYNTAX=1`` to bypass (debug only).
    """
    try:
        p = resolve_inside_root(path)
        # ANTI-TRUNCATION GUARD: refuse to overwrite an EXISTING non-trivial file
        # with a drastically smaller one. A local model that "rewrites" a file it
        # read often drops most of it (e.g. PartiesWorkflow 558→13 lines) — on a
        # real repo that silently destroys code. Force a targeted file_patch/edit
        # instead. Tunable: AIFORGE_TRUNCATE_MIN_LINES / AIFORGE_TRUNCATE_KEEP_FRAC;
        # AIFORGE_ALLOW_TRUNCATION=1 bypasses (e.g. an intentional full rewrite).
        if os.environ.get("AIFORGE_ALLOW_TRUNCATION", "0") not in ("1", "true") \
                and p.exists() and p.is_file():
            try:
                _old = p.read_text(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                _old = ""
            _old_lines = _old.count("\n") + 1 if _old else 0
            _new_lines = content.count("\n") + 1 if content else 0
            try:
                _min = int(os.environ.get("AIFORGE_TRUNCATE_MIN_LINES", "25"))
                _frac = float(os.environ.get("AIFORGE_TRUNCATE_KEEP_FRAC", "0.5"))
            except ValueError:
                _min, _frac = 25, 0.5
            if _old_lines >= _min and _new_lines < _old_lines * _frac:
                return {
                    "ok": False,
                    "error": (f"refused: would shrink existing {path} from "
                              f"{_old_lines} to {_new_lines} lines "
                              f"(>{int((1 - _frac) * 100)}% loss — likely a "
                              "truncated rewrite)."),
                    "hint": ("make the change with file_patch/edit (targeted "
                             "replace) instead of rewriting the whole file; if a "
                             "full rewrite is truly intended, keep ALL existing "
                             "code you are not changing."),
                }
        if os.environ.get("AIFORGE_DOER_SKIP_SYNTAX", "0") not in ("1", "true"):
            ok, err = validate_syntax(path, content)
            if not ok:
                return {
                    "ok": False,
                    "error": f"syntax_invalid: {err}",
                    "hint": (
                        "fix the syntax and call file_write again; "
                        "or call memory_lookup if you need symbol info"
                    ),
                }
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        record_touch(path)
        return {"ok": True, "path": path,
                "bytes": len(content.encode("utf-8"))}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def file_patch(path: str, old_text: str, new_text: str) -> dict:
    """Replace the FIRST occurrence of ``old_text`` with ``new_text``.

    Failure modes: ``not_found`` (file missing), ``old_text_not_found``
    (no match), ``ambiguous_match`` (>1 occurrence — caller passes
    more context to disambiguate).
    """
    try:
        p = resolve_inside_root(path)
        if not p.is_file():
            return {"ok": False, "error": "not_found"}
        body = p.read_text(encoding="utf-8")
        count = body.count(old_text)
        if count == 0:
            return {"ok": False, "error": "old_text_not_found"}
        if count > 1:
            return {"ok": False, "error": "ambiguous_match",
                    "occurrences": count}
        p.write_text(body.replace(old_text, new_text, 1), encoding="utf-8")
        record_touch(path)
        return {"ok": True, "path": path, "replaced": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def list_dir(path: str = "") -> dict:
    """List directory entries under the repo root."""
    try:
        p = resolve_inside_root(path) if path else root()
        if not p.is_dir():
            return {"ok": False, "error": f"not a dir: {path}"}
        entries = []
        for child in sorted(p.iterdir()):
            if child.is_dir():
                kind = "dir"
            elif child.is_file():
                kind = "file"
            else:
                kind = "other"
            entries.append({"name": child.name, "kind": kind})
        return {"ok": True, "path": path or ".", "entries": entries}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}


def run_shell(cmd: str) -> dict:
    """Run a shell command inside the repo root.

    Timeout AIFORGE_SHELL_TIMEOUT (default 600s); output truncated to 8 KB
    per stream so a runaway
    test suite cannot blow up session state.

    Refuses DANGEROUS commands (rm -rf /, fork bombs, disk wipes, …) even in
    the unattended pipeline — the interactive caution gate only covers chat, so
    this is the pipeline's hard floor.
    """
    try:
        from aiforge_core.runtime.tools import command_risk
        verdict = command_risk.assess(cmd)
        if verdict.get("level") == command_risk.DANGEROUS:
            return {"ok": False, "error": "blocked_dangerous_command",
                    "reason": verdict.get("reason", ""), "returncode": -1}
    except Exception as exc:  # noqa: BLE001
        # FAIL CLOSED: if the safety classifier itself errors (bad regex, typo),
        # refuse the command rather than run it unchecked — a broken gate must
        # not turn into arbitrary execution. Override with
        # AIFORGE_RISK_GATE_FAIL_OPEN=1 only if you accept that risk.
        if os.environ.get("AIFORGE_RISK_GATE_FAIL_OPEN", "0") not in ("1", "true", "yes"):
            return {"ok": False, "error": "risk_check_failed",
                    "reason": f"safety classifier error: {exc}", "returncode": -1}
    # Login shell (bash -lc) so the operator's version managers — sdkman,
    # nvm, pyenv, rbenv, cargo — are on PATH; a bare `sh -c` sees only the
    # system defaults (e.g. an old JDK) and builds fail on version mismatch.
    # Generous, env-tunable timeout: an install (sdk/nvm/pyenv/apt) plus a
    # full build does not fit in 90s. Override with AIFORGE_SHELL_TIMEOUT.
    try:
        _sh_timeout = int(os.environ.get("AIFORGE_SHELL_TIMEOUT", "600") or "600")
    except ValueError:
        _sh_timeout = 600
    # Run under bash (not the /bin/sh default) so the doer can `source` a
    # version manager (sdkman/nvm/pyenv) and chain `&&` when it provisions a
    # missing/mismatched toolchain itself. We do NOT auto-activate any manager
    # here — the doer owns toolchain setup via its own commands (see doer prompt).
    _argv = ["bash", "-c", cmd] if shutil.which("bash") else cmd
    try:
        proc = subprocess.run(
            _argv, shell=not isinstance(_argv, list), cwd=root(),
            capture_output=True, timeout=_sh_timeout,
        )
    except subprocess.TimeoutExpired as exc:
        _to_out = (exc.stdout or b"").decode("utf-8", "replace")
        _to_err = (exc.stderr or b"").decode("utf-8", "replace")
        _r = {"ok": False, "error": "timeout",
              "stdout": _to_out[:8000], "stderr": _to_err[:8000]}
        _d = _compact_digest(_to_out, _to_err, None)
        if _d:
            _r["digest"] = _d
        return _r
    out = proc.stdout.decode("utf-8", "replace")
    err = proc.stderr.decode("utf-8", "replace")
    res = {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "stdout": out[:8000], "stderr": err[:8000],
        "truncated": len(out) > 8000 or len(err) > 8000,
    }
    # Signal-first digest (deterministic, no LLM) so a slow model leads with
    # the error lines instead of scrollback. Additive — never replaces output.
    digest = _compact_digest(out, err, proc.returncode)
    if digest:
        res["digest"] = digest
    return res


# ─── Repo grep ─────────────────────────────────────────────────────────


_GREP_DEFAULT_EXCLUDES = (
    ".git", "node_modules", "target", "build", "dist", ".venv", "venv",
    "__pycache__", ".mvn", ".idea", ".gradle",
)


def grep_repo(pattern: str, path: str = ".") -> dict:
    """Recursive regex search over the repo. Returns matching ``{file,
    line, text}`` rows.

    Uses ripgrep when available (10-100x faster on large trees), falls
    back to ``grep -RnE``. Both produce the same shape so the model
    can't tell the difference. Output capped at 200 hits / 8 KB to
    keep the agent context small.

    Args:
      pattern: extended regex (anchors, groups, alternation OK).
      path: search root, repo-relative; default = whole repo.
    """
    if not pattern or not pattern.strip():
        return {"ok": False, "error": "empty pattern"}
    try:
        target = resolve_inside_root(path) if path and path != "." else root()
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
    if not target.exists():
        return {"ok": False, "error": f"not found: {path}"}

    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--no-heading", "--with-filename", "--line-number",
               "--max-count", "200", "--max-filesize", "1M",
               "-e", pattern, str(target)]
        for ex in _GREP_DEFAULT_EXCLUDES:
            cmd[1:1] = ["--glob", f"!{ex}"]
    else:
        excludes = []
        for ex in _GREP_DEFAULT_EXCLUDES:
            excludes += [f"--exclude-dir={ex}"]
        cmd = ["grep", "-RnE", *excludes, "--", pattern, str(target)]

    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=30, cwd=root(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout"}
    except FileNotFoundError as exc:
        return {"ok": False, "error": f"binary missing: {exc}"}

    out = proc.stdout.decode("utf-8", "replace")
    hits: list[dict] = []
    repo_root = str(root())
    for line in out.splitlines()[:200]:
        # rg/grep both emit `path:lineno:text`. Split only twice so a
        # colon in code lands in `text`, not the path/lineno fields.
        parts = line.split(":", 2)
        if len(parts) < 3:
            continue
        file_abs, lineno, text = parts
        rel = file_abs[len(repo_root):].lstrip("/") if file_abs.startswith(repo_root) else file_abs
        hits.append({"file": rel, "line": int(lineno) if lineno.isdigit() else 0,
                     "text": text[:240]})
    return {
        "ok": True,
        "pattern": pattern,
        "path": path or ".",
        "engine": "rg" if rg else "grep",
        "hits": hits,
        "truncated": len(out.splitlines()) > 200,
    }


def read_lines(path: str, start: int = 1, end: int = 0) -> dict:
    """Read a LINE RANGE from a file (1-indexed, inclusive) — for big files you
    don't want whole. ``end=0`` reads to EOF (capped). Read-only."""
    try:
        p = resolve_inside_root(path)
        with open(p, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return {"ok": False, "error": f"not found: {path}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    n = len(lines)
    s = max(1, int(start or 1))
    e = n if not end else min(int(end), n)
    if s > n:
        return {"ok": True, "path": path, "total_lines": n, "text": "",
                "note": f"start {s} past EOF ({n} lines)"}
    chunk = lines[s - 1:e][:5000]                # hard cap the slice
    body = "".join(chunk)[:60000]
    return {"ok": True, "path": path, "start": s, "end": e,
            "total_lines": n, "text": body}
