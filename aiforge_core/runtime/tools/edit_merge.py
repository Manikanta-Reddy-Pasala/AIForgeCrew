"""Edit a Confluence page body or a Jira description WITHOUT losing the rest.

The update tools used to PUT whatever the model sent as the whole body. A model
asked to "add a section" or "fix the dates" sends only the part it changed —
and the page lost everything else. Or it rewrites the page from its own reading
as Markdown, and every table, panel and macro became a paragraph.

So an edit names what it changes (``mode``) and is merged into the LIVE body:

* ``append`` / ``prepend`` — add the fragment at the end / start.
* ``replace_section`` — replace one section, found by its heading text
  (``section``), up to the next heading of the same or a higher level. A
  fragment that starts with its own heading replaces the heading too.
* ``replace_text`` — replace one exact, unique piece of the body (``find``).
* ``replace`` — the whole body. Refused when it would delete most of the text
  or drop tables/macros the page has, unless ``allow_loss`` says the loss is
  what the user asked for.

Two body kinds: ``storage`` (Confluence XHTML) and ``wiki`` (Jira wiki markup).
Pure and dependency-free, so the approval preview shows the exact result.
"""
from __future__ import annotations

import html as _html
import re

MODES = ("replace", "append", "prepend", "replace_section", "replace_text")

# Below this much text a page is short enough that a full rewrite is normal.
_MIN_GUARDED_CHARS = 200
# A full replace keeping less than this share of the text is refused.
_KEEP_RATIO = 0.5

_STORAGE_HEADING = re.compile(r"<h([1-6])\b[^>]*>(.*?)</h\1\s*>", re.I | re.S)
_WIKI_HEADING = re.compile(r"^[ \t]*h([1-6])\.[ \t]+(.*)$", re.M)

# Structures a rewrite silently flattens: count them before and after.
_STORAGE_STRUCTURES = {
    "table": re.compile(r"<table\b", re.I),
    "macro": re.compile(r"<ac:structured-macro\b", re.I),
    "layout": re.compile(r"<ac:layout-section\b", re.I),
    "image": re.compile(r"<ac:image\b", re.I),
    "task list": re.compile(r"<ac:task-list\b", re.I),
}
_WIKI_STRUCTURES = {
    "table": re.compile(r"^[ \t]*\|\|", re.M),
    "code block": re.compile(r"\{code\b", re.I),
    "panel": re.compile(r"\{(panel|info|note|warning|tip|quote|noformat)\b", re.I),
    "image": re.compile(r"![^!\s|]+\.(png|jpe?g|gif|svg|webp)[^!\n]*!", re.I),
}


class EditError(ValueError):
    """The edit cannot be applied as asked; the message tells the model how."""


def _text(body: str, kind: str) -> str:
    """The readable text of ``body``, whitespace-normalised."""
    if kind == "storage":
        body = _html.unescape(re.sub(r"<[^>]+>", " ", body))
    else:
        body = re.sub(r"\{[^}\n]*\}|^[ \t]*h[1-6]\.|[|*_#]", " ", body, flags=re.M)
    return " ".join(body.split())


def _norm(s: str) -> str:
    return " ".join(_html.unescape(re.sub(r"<[^>]+>", " ", s)).split()).casefold()


# Code a heading-lookalike can hide in: a macro's CDATA body (storage), a
# {code}/{noformat} block (wiki). Masked before looking for headings.
_STORAGE_OPAQUE = re.compile(r"<!\[CDATA\[.*?\]\]>", re.S)
_WIKI_OPAQUE = re.compile(r"\{(code|noformat)(?::[^}]*)?\}.*?\{\1\}", re.S | re.I)

# One storage tag (or CDATA, skipped whole).
_TAG = re.compile(r"<!\[CDATA\[.*?\]\]>|<(/?)([A-Za-z][\w:.-]*)\b[^>]*?(/?)>", re.S)
_VOID = {"br", "hr", "img", "col", "input", "meta", "link", "wbr", "area", "base"}


def _mask(body: str, kind: str) -> str:
    """``body`` with code regions blanked (same length, so offsets hold)."""
    rx = _STORAGE_OPAQUE if kind == "storage" else _WIKI_OPAQUE
    return rx.sub(lambda m: " " * len(m.group(0)), body)


def _headings(body: str, kind: str) -> list[tuple[int, int, int, str]]:
    """(start, end, level, text) of every heading, in order."""
    rx = _STORAGE_HEADING if kind == "storage" else _WIKI_HEADING
    masked = _mask(body, kind)
    return [(m.start(), m.end(), int(m.group(1)), _norm(body[m.start(2):m.end(2)]))
            for m in rx.finditer(masked)]


def _storage_section_end(body: str, head_end: int, level: int) -> int:
    """Where the section opened by a level-``level`` heading ends: the next
    heading of that level or higher at the SAME nesting depth, or the close of
    the element the heading sits in (a layout cell, a macro body, a table
    cell) — never past it, which would cut the page's structure apart."""
    depth = 0
    for m in _TAG.finditer(body, head_end):
        if m.group(2) is None:                       # CDATA
            continue
        closing, name, selfclose = m.group(1), m.group(2).lower(), m.group(3)
        if selfclose or name in _VOID:
            continue
        if closing:
            if depth == 0:
                return m.start()                     # the container closes
            depth -= 1
            continue
        hm = re.fullmatch(r"h([1-6])", name)
        if depth == 0 and hm and int(hm.group(1)) <= level:
            return m.start()
        depth += 1
    return len(body)


def _find_section(body: str, kind: str, section: str) -> tuple[int, int, int]:
    """(heading start, heading end, section end) for ``section``: an exact
    heading match first, else the ONE heading that contains it."""
    want = _norm(section).lstrip("#").strip()
    heads = _headings(body, kind)
    hits = [h for h in heads if h[3] == want] or [h for h in heads if want in h[3]]
    if not hits:
        names = ", ".join(repr(h[3]) for h in heads[:20]) or "none"
        raise EditError(f"section {section!r} not found; the headings are: {names}")
    if len(hits) > 1:
        raise EditError(f"section {section!r} matches {len(hits)} headings; "
                        "give the full heading text")
    start, end, level, _ = hits[0]
    if kind == "storage":
        return start, end, _storage_section_end(body, end, level)
    after = [h[0] for h in heads if h[0] > start and h[2] <= level]
    return start, end, (after[0] if after else len(body))


def _starts_with_heading(fragment: str, kind: str) -> bool:
    rx = (re.compile(r"^\s*<h[1-6]\b", re.I) if kind == "storage"
          else re.compile(r"^\s*h[1-6]\.[ \t]"))
    return bool(rx.match(fragment))


def _joiner(kind: str) -> str:
    return "" if kind == "storage" else "\n\n"


def merge(current: str, fragment: str, mode: str, *, kind: str,
          section: str | None = None, find: str | None = None) -> str:
    """``current`` with ``fragment`` applied as ``mode``. Raises EditError."""
    current = current or ""
    mode = (mode or "replace").strip().lower()
    if mode not in MODES:
        raise EditError(f"unknown mode {mode!r}; use one of {', '.join(MODES)}")
    if mode == "replace":
        return fragment
    if mode == "append":
        return (current.rstrip() + _joiner(kind) + fragment) if current.strip() else fragment
    if mode == "prepend":
        return (fragment + _joiner(kind) + current.lstrip()) if current.strip() else fragment
    if mode == "replace_section":
        if not (section or "").strip():
            raise EditError("mode replace_section needs 'section' (the heading text)")
        start, head_end, end = _find_section(current, kind, section)
        keep_from = start if _starts_with_heading(fragment, kind) else head_end
        sep = "" if kind == "storage" else "\n"
        tail = current[end:]
        return (current[:keep_from] + sep + fragment.strip()
                + ("\n\n" if kind == "wiki" and tail else "") + tail)
    # replace_text
    if not find:
        raise EditError("mode replace_text needs 'find' (exact text copied from the body)")
    n = current.count(find)
    if n == 0:
        raise EditError("'find' text not found in the current body; copy it "
                        "exactly from the read result (markup included)")
    if n > 1:
        raise EditError(f"'find' text occurs {n} times; include more "
                        "surrounding text so it is unique")
    return current.replace(find, fragment, 1)


def loss(current: str, merged: str, *, kind: str) -> str | None:
    """Why ``merged`` would lose content ``current`` has, or None."""
    rules = _STORAGE_STRUCTURES if kind == "storage" else _WIKI_STRUCTURES
    dropped = []
    for name, rx in rules.items():
        before, after = len(rx.findall(current or "")), len(rx.findall(merged or ""))
        if after < before:
            dropped.append(f"{before - after} of {before} {name}(s)")
    old, new = len(_text(current or "", kind)), len(_text(merged or "", kind))
    shrunk = old >= _MIN_GUARDED_CHARS and new < old * _KEEP_RATIO
    if not dropped and not shrunk:
        return None
    what = []
    if shrunk:
        what.append(f"{100 - round(100 * new / old)}% of the text "
                    f"({old} → {new} chars)")
    what += dropped
    return "this would remove " + ", ".join(what) + " from the current body"


def apply_edit(current: str, fragment: str, args: dict, *, kind: str) -> str:
    """Merge per ``args`` (mode/section/find/allow_loss) and guard a full
    replace. Returns the new body; raises EditError with the way forward."""
    mode = (args.get("mode") or "replace").strip().lower()
    merged = merge(current, fragment, mode, kind=kind,
                   section=args.get("section"), find=args.get("find"))
    if mode == "replace" and not _truthy(args.get("allow_loss")):
        why = loss(current, merged, kind=kind)
        if why:
            raise EditError(
                f"refused: {why}. To change part of it use mode 'append', "
                "'prepend', 'replace_section' (with 'section') or "
                "'replace_text' (with 'find') — only the part you send "
                "changes. For a whole-body rewrite, send the COMPLETE body "
                "with every table and macro kept; only if the user asked for "
                "that content to go, retry with allow_loss: true.")
    return merged


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ("1", "true", "yes", "on")


__all__ = ["EditError", "MODES", "apply_edit", "loss", "merge"]
