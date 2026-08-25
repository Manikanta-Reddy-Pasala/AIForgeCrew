"""Convert an agent-authored page body into Confluence **storage format** (XHTML).

Confluence renders page bodies as storage-format XHTML — NOT Markdown. The model
naturally emits Markdown (``**bold**``, ``## Heading``, ``- bullet``), which
Confluence then shows LITERALLY. :func:`md_to_storage` converts the Markdown
constructs agents actually produce into storage XHTML so the page renders right.

Fenced code blocks (```` ``` ````) and images (``![]`` / ``<img>``) are LEFT
ALONE here — ``confluence._storagify_media`` rewrites those into the proper
``<ac:structured-macro>`` / attachment forms afterwards. A body that already
looks like storage XHTML (has block tags) passes through unchanged.

Dependency-free (deploy-anywhere): a small, targeted converter, not a full
Markdown parser.
"""
from __future__ import annotations

import html as _html
import re

# Already-storage signal: real block/format tags or a Confluence macro.
_STORAGE_HINT = re.compile(r"<(p|h[1-6]|ul|ol|li|strong|em|a|table|ac:)\b", re.I)
_FENCE = re.compile(r"```.*?```", re.DOTALL)


def md_to_storage(text):
    """Return ``text`` as Confluence storage XHTML. Markdown → converted; a body
    that already looks like storage XHTML → unchanged. ``None``/"" pass through."""
    if not text:
        return text
    if _STORAGE_HINT.search(text):
        return text                      # already storage — don't double-convert
    # Protect fenced code + images: replace with placeholders, convert, restore.
    saved: list[str] = []

    def _stash(m):
        saved.append(m.group(0))
        return f"\x00{len(saved) - 1}\x00"
    protected = _FENCE.sub(_stash, text)
    protected = re.sub(r"!\[[^\]]*\]\([^)]*\)",
                       _stash, protected)         # ![alt](src) images
    protected = re.sub(r"<img\b[^>]*>", _stash, protected, flags=re.I)

    out = _blocks_to_storage(protected)

    for i, chunk in enumerate(saved):             # restore placeholders verbatim
        out = out.replace(f"\x00{i}\x00", chunk)
    return out


def _inline(s: str) -> str:
    """Inline markdown → storage XHTML on ONE line's text. Escapes bare XHTML
    special chars first so user text can't inject tags, then re-introduces the
    intended emphasis/code/link tags."""
    s = _html.escape(s, quote=False)
    # inline code `x` first (so bold/italic never touch its contents)
    s = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", s)
    # links [t](u) -> <a href="u">t</a>
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    # bold **x** / __x__ -> <strong>
    s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"__([^_]+)__", r"<strong>\1</strong>", s)
    # italic *x* / _x_ -> <em>  (after bold so ** isn't half-eaten)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
    s = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"<em>\1</em>", s)
    return s


def _consume_list(lines: list[str], i: int) -> "tuple[str, int]":
    """A run of SAME-TYPE list items starting at ``lines[i]`` → (``<ul>/<ol>``
    html, next index). An ordered run and an unordered run stay separate lists."""
    ordered = bool(re.match(r"^\s*\d+\.\s+", lines[i]))
    item_re = r"^\s*\d+\.\s+" if ordered else r"^\s*[-*+]\s+"
    items: list[str] = []
    while i < len(lines) and re.match(item_re, lines[i]):
        it = re.sub(item_re, "", lines[i]).strip()
        items.append(f"<li>{_inline(it)}</li>")
        i += 1
    tag = "ol" if ordered else "ul"
    return f"<{tag}>{''.join(items)}</{tag}>", i


def _block_at(lines: list[str], i: int) -> "tuple[str | None, int]":
    """The block rendered from ``lines[i]`` → (html or None for a blank line,
    next index). Handles placeholders, headings, list runs and paragraphs."""
    raw = lines[i].strip()
    if not raw:
        return None, i + 1
    if re.fullmatch(r"\x00\d+\x00", raw):
        return raw, i + 1                 # protected fence/image placeholder
    hm = re.match(r"^(#{1,6})[ \t]+(.*)$", raw)
    if hm:
        lvl = len(hm.group(1))
        return f"<h{lvl}>{_inline(hm.group(2).strip())}</h{lvl}>", i + 1
    if re.match(r"^\s*[-*+]\s+", lines[i]) or re.match(r"^\s*\d+\.\s+", lines[i]):
        return _consume_list(lines, i)
    return f"<p>{_inline(raw)}</p>", i + 1


def _blocks_to_storage(s: str) -> str:
    """Line-oriented block conversion: headings, ordered/unordered lists (runs
    grouped into <ul>/<ol>), and paragraphs. Blank lines separate paragraphs."""
    lines = s.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        block, i = _block_at(lines, i)
        if block is not None:
            out.append(block)
    return "\n".join(out)


__all__ = ["md_to_storage"]
