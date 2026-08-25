from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .._shell import _workspace_root

_GIT_TOPLEVEL_CACHE: dict[str, str | None] = {}


def _git_toplevel(cwd: str | None) -> str | None:
    """Repo root for ``cwd`` (``git rev-parse --show-toplevel``), cached and
    soft-failing to None outside a work tree. Lets a SUBDIR resolve the same
    repo key as the root (gap M3)."""
    if not cwd:
        return None
    key = str(cwd)
    if key in _GIT_TOPLEVEL_CACHE:
        return _GIT_TOPLEVEL_CACHE[key]
    top: str | None = None
    try:
        out = subprocess.run(
            ["git", "-C", key, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0:
            top = out.stdout.strip() or None
    except Exception:  # noqa: BLE001 — recall must never break on git
        top = None
    _GIT_TOPLEVEL_CACHE[key] = top
    return top


def _chat_repo_key(cwd: str | None) -> str:
    """Repo key for chat recall — resolves the GIT-TOPLEVEL basename (so a
    subdir recalls the same repo as the root), falling back to the raw cwd
    basename, then ``AIFORGE_AFM_REPO``, then the literal ``"repo"`` (gap M3).
    Note ``repo_key`` is always truthy for a real path, so its ``or env``
    fallback was dead — we chain the env explicitly here."""
    from aiforge_core.runtime import repo_ident as _ri
    return _ri.repo_name(cwd, sentinel="repo")


# Elaboration prompts — turn a user's rough input into a well-structured
# playbook BODY (no frontmatter; write_skill/write_workflow add that). Local
# models often emit a thin one-liner as the body; running it through the model
# once server-side guarantees a formatted, elaborated artifact.
_ELABORATE_PROMPT = {
    "skill": ("Rewrite the following into a clear, reusable SKILL body: a short "
              "intro line then concise numbered/bulleted steps the agent "
              "follows. Keep the user's intent; add the obvious missing detail. "
              "Output ONLY the markdown body — NO YAML frontmatter, no name."),
    "workflow": ("Rewrite the following into a WORKFLOW body: numbered "
                 "end-to-end steps, each concrete and in dependency order, with "
                 "a final done-check. Keep the user's intent; fill obvious gaps. "
                 "Output ONLY the markdown body — NO frontmatter."),
    "rule": ("Rewrite the following into a coding RULE: a '# Title' line then "
             "tight imperative bullet points the agent must follow. Keep the "
             "intent; make each bullet testable. Output ONLY the markdown."),
}


def _already_structured(body: str) -> bool:
    """True for a substantial, already-structured doc (>=400 chars, multi-line,
    with list/heading markers) — leave those alone to avoid churn."""
    return len(body) >= 400 and ("\n" in body) and any(
        m in body for m in ("- ", "1.", "# ", "* "))


def _strip_markdown_fence(out: str) -> str:
    """Strip a stray ```markdown fence the model may have wrapped output in."""
    if not out.startswith("```"):
        return out
    parts = out.split("```")
    if len(parts) >= 2:
        out = parts[1]
        if out.lower().lstrip().startswith("markdown"):
            out = out.lstrip()[8:]
        out = out.strip()
    return out


def _elaborate_body(kind: str, body: str, *, name: str = "",
                    description: str = "") -> str:
    """Format+elaborate a rough ``body`` via the model. Best-effort: returns the
    ORIGINAL body on any failure/empty, and skips when disabled or the body is
    already substantial (>= 400 chars with structure) so we don't over-rewrite a
    good doc. Off with AIFORGE_BUILDER_ELABORATE=0."""
    body = (body or "").strip()
    if os.environ.get("AIFORGE_BUILDER_ELABORATE", "1") in ("0", "false", "no"):
        return body
    prompt = _ELABORATE_PROMPT.get(kind)
    if not prompt or not body or _already_structured(body):
        return body
    ctx = (f"Name: {name}\n" if name else "") + \
          (f"Purpose: {description}\n" if description else "")
    try:
        from aiforge_core.llm import client as _llm
        out = _llm.complete("architect", [
            {"role": "system", "content": prompt},
            {"role": "user", "content": (ctx + "\nInput:\n" + body).strip()},
        ], max_tokens=900, temperature=0.2,
            timeout_s=int(os.environ.get("AIFORGE_BUILDER_ELABORATE_TIMEOUT_S", "45")))
        return _strip_markdown_fence((out or "").strip()) or body
    except Exception:  # noqa: BLE001 — elaboration is best-effort, never block save
        return body


# ─────────────────────────── shared "strong" tools ──────────────────────────
# The OpenHands-parity tools (editor with undo + syntax-check, LSP, typecheck,
# format, test-runner) lived only in the ADK team pipeline. These thin adapters
# expose them to the deploy-anywhere chat agent too. They resolve through
# sandbox.root(); the dispatch loop scopes that override to the WORKSPACE root
# (set+reset in finally) for exactly these names — so a path can't escape an
# AIFORGE_WORKSPACE_DIR jail and a reused thread can't leak the dir.
# ipython (execute_ipython_cell) IS exposed to chat for Claude-Code/Cursor
# parity, but — because it runs arbitrary code in a kernel — it is
# approval-gated (in tool_policy._DEFAULT_ASK → ASK in Act mode, blocked in
# Plan mode) AND cwd-jailed here, so it can't run unapproved or escape the
# AIFORGE_WORKSPACE_DIR root the way the old unmanaged version did.
_ROOT_SCOPED_TOOLS = {"editor", "typecheck", "format", "lsp", "run_tests",
                      "execute_ipython_cell"}


def _scoped_root(cwd: str) -> str:
    """Root the strong tools should resolve against. Use the session ``cwd`` (so
    they hit the SAME files as file_read/file_write/multi_edit, and each parallel
    worktree stays isolated). Only when an AIFORGE_WORKSPACE_DIR jail is set AND
    cwd escapes it do we clamp to the jail root — so the strong tools can't write
    outside the jail, without collapsing every subtask onto one shared dir."""
    try:
        ws = _workspace_root()
        if ws is None:
            return cwd
        c = Path(cwd).expanduser().resolve()
        return str(c) if (c == ws or ws in c.parents) else str(ws)
    except Exception:  # noqa: BLE001
        return cwd


def _coerce_int(v, default=None):
    try:
        return int(v) if v is not None and str(v).strip() != "" else default
    except (TypeError, ValueError):
        return default


def _git_cli(argv: list, cwd: str, timeout: int = 30) -> dict:
    import subprocess
    try:
        r = subprocess.run(["git", *argv], cwd=cwd or ".", capture_output=True,
                           text=True, timeout=timeout)
        return {"ok": r.returncode == 0, "code": r.returncode,
                "stdout": (r.stdout or "")[-8000:], "stderr": (r.stderr or "")[-2000:]}
    except FileNotFoundError:
        return {"ok": False, "error": "git_not_installed"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _chat_run_id(cwd: str) -> str:
    """Stable per-workspace id so the browser tab / IPython kernel PERSIST
    across chat turns.

    Tool handlers only receive ``(args, cwd)`` — the chat ``session_id`` is not
    threaded down to them — so we derive a deterministic id from ``cwd``. Using
    a content hash (not the salted builtin ``hash``) keeps it stable across
    process restarts, so a reconnecting session reattaches to the same tab.
    """
    import hashlib
    digest = hashlib.md5((cwd or ".").encode("utf-8")).hexdigest()[:12]
    return f"chat-{digest}"
