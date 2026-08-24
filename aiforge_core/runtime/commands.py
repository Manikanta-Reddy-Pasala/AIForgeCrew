"""User-defined slash commands — reusable prompt templates (Claude Code /
Cursor parity, LOCAL files only).

Claude Code and Cursor let a user drop a markdown file into a ``commands``
directory and invoke it as ``/name args`` in chat. AIForge already has
skills / workflows (auto-selected by relevance), but no user-*invoked*
prompt templates. This module adds them, mirroring the skills loader's
file-discovery (:mod:`aiforge_core.runtime.skills`).

A command is a single markdown file whose STEM is the command name::

    <repo>/.aiforge/commands/deploy.md        ->   /deploy

The file body is a prompt template. On invocation, ``$ARGUMENTS`` expands
to the full argument string the user typed after ``/deploy`` and ``$1``,
``$2`` … to whitespace-split positionals. Placeholders with no matching
argument are left as-is. Optional YAML frontmatter (``description``) is
parsed like a skill; the body AFTER the frontmatter is the template.

Roots (all merged; repo-local overrides global, exactly like skills):
    $AIFORGE_COMMANDS_DIR or ~/.aiforge/commands/   (global)
    <repo>/.aiforge/commands/
    <repo>/.claude/commands/

Everything is best-effort / fail-open: a missing dir yields no commands, a
malformed file is skipped, and a message that is not a known ``/command``
returns ``None`` from :func:`expand` so the caller uses the raw text.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from aiforge_core.config.paths import config_dir

try:
    import yaml
except Exception:  # noqa: BLE001
    yaml = None  # type: ignore

# `[ \t]*` not `\s*`: `\s` MATCHES the newline, so `---\s*\n` could split a
# run of blank lines many ways — the super-linear case. What is actually
# meant is "trailing spaces/tabs on the --- line".
_FRONTMATTER_RE = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)$", re.DOTALL)
_REPO_SUBDIRS = (".aiforge/commands", ".claude/commands")
# A slash command token: leading '/', then at least one name char. A bare
# '/' followed by a space (e.g. "/ 2 + 2") deliberately does NOT match, so a
# real message that merely starts with a slash is never swallowed.
_NAME_RE = re.compile(r"^/([A-Za-z0-9_.-]+)")
_POSITIONAL_RE = re.compile(r"\$(\d+)")
# Built-in commands that need no user file (so /help works with zero files).
_BUILTINS = ("help", "commands")


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    body: str
    source: str = ""


def _global_dir() -> Path:
    raw = os.environ.get("AIFORGE_COMMANDS_DIR")
    if raw:
        return Path(raw).expanduser()
    # Same config dir as the rest of the app (AIFORGE_CONFIG_DIR); a raw
    # Path.home() diverges from the operator's configured/mounted dir.
    cfg = str(config_dir())
    return Path(cfg) / "commands"


def _repo_root(cwd: str | None) -> str | None:
    from aiforge_core.runtime import request_context
    return (request_context.get_workspace_dir() or cwd
            or request_context.get_repo_root() or None)


def _parse_command_md(text: str) -> tuple[str, str]:
    """Return ``(description, body)`` from a command file. The body is the
    text AFTER any YAML frontmatter (``description`` is the only recognised
    key — the command NAME is always the file stem, so invocation is
    predictable)."""
    m = _FRONTMATTER_RE.match(text or "")
    meta: dict = {}
    body = text or ""
    if m:
        body = m.group(2)
        if yaml is not None:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except Exception:  # noqa: BLE001
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
    desc = str(meta.get("description") or "").strip()
    return desc, (body or "").strip()


def _scan_dir(root: Path) -> list[Command]:
    """Read ``<root>/*.md`` — one command per markdown file (stem = name)."""
    out: list[Command] = []
    if not root.exists():
        return out
    try:
        for child in sorted(root.iterdir()):
            if child.suffix != ".md" or not child.is_file():
                continue
            try:
                desc, body = _parse_command_md(
                    child.read_text(encoding="utf-8", errors="ignore"))
            except Exception:  # noqa: BLE001
                continue
            if not body:
                continue
            out.append(Command(name=child.stem, description=desc,
                               body=body, source=str(child)))
    except Exception:  # noqa: BLE001
        return out
    return out


def load(cwd: str | None = None) -> dict[str, Command]:
    """Global + repo-local commands, de-duped by name (repo-local / later
    wins on a clash). ``name -> Command``. Best-effort."""
    by_name: dict[str, Command] = {}
    for cmd in _scan_dir(_global_dir()):
        by_name[cmd.name] = cmd
    root = _repo_root(cwd)
    if root:
        for sub in _REPO_SUBDIRS:
            for cmd in _scan_dir(Path(root) / sub):
                by_name[cmd.name] = cmd
    return by_name


def _substitute(body: str, arg_str: str) -> str:
    """``$ARGUMENTS`` → full arg string; ``$1``/``$2``… → positionals
    (whitespace-split). Out-of-range positionals are left verbatim."""
    args = arg_str.split()
    out = body.replace("$ARGUMENTS", arg_str)

    def _repl(m: re.Match) -> str:
        idx = int(m.group(1))
        if 1 <= idx <= len(args):
            return args[idx - 1]
        return m.group(0)          # no such arg → leave "$N" untouched

    return _POSITIONAL_RE.sub(_repl, out)


def _help_text(cmds: dict[str, Command]) -> str:
    lines = ["Available slash commands:"]
    for name in sorted(cmds):
        desc = cmds[name].description
        lines.append(f"  /{name}" + (f" — {desc}" if desc else ""))
    if not cmds:
        lines.append("  (no custom commands yet — add markdown files under "
                     ".aiforge/commands/ or .claude/commands/)")
    lines.append("  /help — list available commands")
    return "\n".join(lines)


def expand(text: str, cwd: str | None = None) -> str | None:
    """If ``text`` begins with ``/<name>`` and ``<name>`` is a known command
    (or the built-in ``/help`` / ``/commands``), return the expanded template.
    Otherwise return ``None`` — the caller then uses the raw text unchanged
    (so ordinary messages, ``/`` typos, and unknown ``/name`` all pass
    through verbatim)."""
    if not text:
        return None
    stripped = text.strip()
    m = _NAME_RE.match(stripped)
    if not m:
        return None
    name = m.group(1)
    arg_str = stripped[m.end():].lstrip()
    cmds = load(cwd)
    if name in cmds:
        return _substitute(cmds[name].body, arg_str)
    if name in _BUILTINS:
        return _help_text(cmds)
    return None


def list_commands(cwd: str | None = None) -> list[dict]:
    """``[{name, description, source}]`` for a UI / ``/help`` listing."""
    cmds = load(cwd)
    return [{"name": c.name, "description": c.description, "source": c.source}
            for c in sorted(cmds.values(), key=lambda c: c.name)]


def is_builtin(name: str) -> bool:
    """True for the zero-file built-ins (``help`` / ``commands``)."""
    return name in _BUILTINS


__all__ = ["Command", "load", "expand", "list_commands", "is_builtin"]
