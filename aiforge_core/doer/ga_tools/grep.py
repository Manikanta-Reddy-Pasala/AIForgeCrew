"""Grep tool — ripgrep-backed code search.

Doer searches inside file contents quickly. Used to locate API
usages ('find example of Aggregation.group in this repo') without
launching a sub-agent. Faster + more focused than ask_explorer
for simple lookups.

Returns matching ``path:line:content`` rows, capped.
"""
from __future__ import annotations

import shutil
import subprocess

SCHEMA = {
    "type": "function",
    "function": {
        "name": "grep",
        "description": (
            "Search file contents for a regex within the worktree. "
            "Returns up to 50 matches as 'path:line: content'. "
            "Use to find API usages ('Aggregation\\.group'), TODO "
            "markers, existing patterns to copy. Faster than "
            "ask_explorer for simple lookups; honours .gitignore."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "regex": {
                    "type": "string",
                    "description": "PCRE-flavour regex.",
                },
                "glob": {
                    "type": "string",
                    "description": (
                        "Restrict to a file pattern, "
                        "e.g. '**/*.java'."
                    ),
                },
                "max_results": {
                    "type": "integer",
                    "default": 50,
                },
            },
            "required": ["regex"],
        },
    },
}


def _ripgrep(worktree: str, regex: str, glob: str | None,
             cap: int) -> list[str]:
    rg = shutil.which("rg")
    if not rg:
        return []
    cmd = [rg, "-n", "--no-heading", "--max-count", str(cap),
           "--max-columns", "200"]
    if glob:
        cmd += ["-g", glob]
    cmd += [regex]
    proc = subprocess.run(
        cmd, cwd=worktree, capture_output=True, text=True, timeout=15,
    )
    return [line for line in proc.stdout.splitlines() if line][:cap]


def handle(worktree: str, args: dict) -> str:
    regex = (args.get("regex") or "").strip()
    if not regex:
        return "[grep] empty regex"
    glob = (args.get("glob") or "").strip() or None
    cap = int(args.get("max_results") or 50)
    cap = max(1, min(cap, 200))
    if not shutil.which("rg"):
        return ("[grep] ripgrep (rg) not installed — install via "
                "'apt install ripgrep' or fall back to ask_explorer.")
    rows = _ripgrep(worktree, regex, glob, cap)
    if not rows:
        return f"[grep] no matches for {regex!r}"
    header = f"[grep] {regex!r} → {len(rows)} match"
    if len(rows) == cap:
        header += " (truncated)"
    if glob:
        header += f" in {glob}"
    return header + "\n" + "\n".join(rows)
