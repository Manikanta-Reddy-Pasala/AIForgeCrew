"""Rendering the command table — the only thing `help` does.

Three surfaces, one source: `aiforge help`, `aiforge <cmd> -h` and `/help`
inside the chat. They cannot drift, because none of them owns any text.
"""

from __future__ import annotations

from . import commands as tbl
from .colors import Palette


def _row(pal: Palette, left: str, right: str, width: int) -> str:
    return f"  {pal(left.ljust(width), 'head')}  {right}"


def top_help(pal: Palette, *, version: str) -> str:
    width = max(len(f"{c.name} {c.usage}".strip()) for c in tbl.TOP)
    out = [
        f"{pal('aiforge', 'head')} {pal(version, 'dim')} — a terminal for AIForge.",
        "",
        f"{pal('usage', 'head')}  aiforge [flags] [message]        a chat in this folder",
        "         aiforge <command> [args]",
        "",
        pal("commands", "head"),
    ]
    for cmd in tbl.TOP:
        out.append(_row(pal, f"{cmd.name} {cmd.usage}".strip(), cmd.help, width))
    out += ["", pal("flags", "head")]
    fw = max(len(f) for f, _ in tbl.GLOBAL_FLAGS)
    for flag, text in tbl.GLOBAL_FLAGS:
        out.append(_row(pal, flag, text, fw))
    out += [
        "",
        pal("the sandbox starts itself on first use; `aiforge help <command>` has the detail.",
            "dim"),
    ]
    return "\n".join(out)


def command_help(pal: Palette, name: str) -> str:
    cmd = tbl.by_name(name, tbl.TOP) or tbl.by_name(name, tbl.SLASH) \
        or tbl.by_name("/" + name, tbl.SLASH)
    if cmd is None:
        known = ", ".join(tbl.top_names())
        return f"{pal('✗', 'fail')} no such command: {name}\n  try one of: {known}"
    out = [f"{pal(cmd.name, 'head')} {pal(cmd.usage, 'dim')}", "", f"  {cmd.help}"]
    if cmd.long:
        out += [""] + ["  " + line for line in cmd.long.splitlines()]
    if cmd.choices:
        out += ["", "  " + pal("values: ", "dim") + ", ".join(cmd.choices)]
    if cmd.examples:
        out += ["", pal("  examples", "head")]
        out += ["    " + pal(e, "code") for e in cmd.examples]
    return "\n".join(out)


def slash_help(pal: Palette) -> str:
    width = max(len(f"{c.name} {c.usage}".strip()) for c in tbl.SLASH)
    out = [pal("commands", "head")]
    for cmd in tbl.SLASH:
        out.append(_row(pal, f"{cmd.name} {cmd.usage}".strip(), cmd.help, width))
    out += ["", pal("keys", "head")]
    kw = max(len(k) for k, _ in tbl.KEYS)
    for key, text in tbl.KEYS:
        out.append(_row(pal, key, text, kw))
    out += ["", pal("anything else is a message. /help <command> for one command.", "dim")]
    return "\n".join(out)
