"""Lightweight regex entity extraction (pure, KISS)."""
from __future__ import annotations

import re


# ─── M4: lightweight entity extraction (pure, regex KISS) ─────────────

# Code-file extensions we treat a bare token as a "file" for even when it
# has no slash (e.g. ``config.yaml``).
_CODE_EXTS = (
    "py", "java", "js", "ts", "tsx", "jsx", "go", "rs", "rb", "c", "h",
    "cpp", "hpp", "cs", "kt", "scala", "swift", "php", "sql", "sh", "yaml",
    "yml", "json", "toml", "xml", "html", "css", "md", "txt", "cfg", "ini",
    "properties", "gradle", "lock", "env",
)

_RE_URL = re.compile(r"https?://[^\s<>\"')]+")
_RE_TICKET = re.compile(r"\b[A-Z]{2,}-\d+\b")
_RE_ENV = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b")
_RE_FILE = re.compile(
    r"\b[\w./\\-]+/[\w./\\-]+"                       # contains a slash
    r"|\b[\w-]+\.(?:" + "|".join(_CODE_EXTS) + r")\b"  # or code ext
)
# fqname: A::b  or  pkg.func / Class.method (alnum dotted, 2+ parts).
_RE_SYMBOL = re.compile(
    r"\b[A-Za-z_][\w]*(?:::[A-Za-z_][\w]*)+\b"       # foo::bar
    r"|\b[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)+\b"      # foo.bar.baz
)


def extract_entities(text: str) -> list[dict]:
    """Pull structured entities out of free text with cheap regexes.

    Returns a deduped list of ``{"type": ..., "value": ...}`` where type
    is one of ``url``, ``file``, ``ticket``, ``env``, ``symbol``. KISS:
    no NLP, no model — just patterns. Order is stable (URLs first, then
    files, tickets, env vars, symbols) and exact-(type,value) duplicates
    are collapsed. Matched URLs/files are masked before symbol detection
    so ``example.com`` inside a URL isn't double-counted as a symbol."""
    text = text or ""
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, value: str) -> None:
        value = value.strip()
        if not value:
            return
        key = (kind, value)
        if key in seen:
            return
        seen.add(key)
        out.append({"type": kind, "value": value})

    masked = text
    for m in _RE_URL.finditer(text):
        _add("url", m.group(0))
    masked = _RE_URL.sub(" ", masked)
    for m in _RE_FILE.finditer(masked):
        _add("file", m.group(0))
    file_masked = _RE_FILE.sub(" ", masked)
    for m in _RE_TICKET.finditer(text):
        _add("ticket", m.group(0))
    for m in _RE_ENV.finditer(text):
        _add("env", m.group(0))
    # symbols last, over text with URLs + files stripped so we don't
    # re-flag dotted file/host tokens as symbols.
    for m in _RE_SYMBOL.finditer(file_masked):
        _add("symbol", m.group(0))
    return out
