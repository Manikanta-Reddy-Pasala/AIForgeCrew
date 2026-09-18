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

import re

# Already-storage signal: real block/format tags or a Confluence macro. Looked
# for OUTSIDE code (fences, `spans`), so Markdown that merely mentions `<div>`
# or `List<String>` is still converted.
_STORAGE_HINT = re.compile(r"<(p|h[1-6]|ul|ol|li|strong|em|a|table|ac:)\b", re.I)
# Any storage tag at all — a one-line replace_text body copied from the page
# (`<td>…</td>`, `<time …/>`, `<code>`) must pass through untouched.
_STORAGE_TAG = re.compile(
    r"</?(p|h[1-6]|ul|ol|li|strong|em|a|table|tbody|thead|tr|td|th|pre|code|"
    r"blockquote|hr|br|span|div|time|u|s|sub|sup|ac:[\w-]+|ri:[\w-]+)\b[^<]*>", re.I)
# An "&" that does NOT start an entity the body already carries (&amp;, &nbsp;,
# &#8377;) — escaping those again showed "&amp;amp;" on the page. Only real
# entity names count: "AT&T;" is escaped (an undefined entity is a 400).
_ENTITIES = ("amp|lt|gt|quot|apos|nbsp|ndash|mdash|hellip|lsquo|rsquo|ldquo|"
             "rdquo|bull|middot|times|divide|copy|reg|trade|deg|plusmn|euro|"
             "pound|yen|cent|sect|para|laquo|raquo|larr|rarr|uarr|darr|harr|"
             "check|zwj|zwnj|shy|ensp|emsp|thinsp")
_BARE_AMP = re.compile(r"&(?!(?:" + _ENTITIES + r"|#\d+|#x[0-9a-fA-F]+);)")
_CODE_SPANS = re.compile(r"```.*?```|`[^`\n]+`", re.S)


def _escape(s: str) -> str:
    """XHTML-escape plain text without double-escaping existing entities."""
    return _BARE_AMP.sub("&amp;", s).replace("<", "&lt;").replace(">", "&gt;")


def _is_storage(text: str) -> bool:
    return bool(_STORAGE_HINT.search(_CODE_SPANS.sub(" ", text)))


_FENCE = re.compile(r"```.*?```", re.DOTALL)
_ORDERED_ITEM = r"^\s*\d+\.\s+"  # markdown ordered-list item: "1. ", "2. ", ...


def md_to_storage(text):
    """Return ``text`` as Confluence storage XHTML. Markdown → converted; a body
    that already looks like storage XHTML → unchanged. ``None``/"" pass through."""
    if not text:
        return text
    if _is_storage(text):
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
    intended emphasis/code/link tags.

    Code spans and links are set aside BEFORE emphasis: the italic pass used to
    run over them, so `get_user_by_id` became ``get<em>user</em>by…`` with the
    tags crossing the ``<code>`` (invalid XHTML — Confluence refuses the whole
    body) and a URL's ``my_run_book`` grew an ``<em>`` inside its href. Italic
    underscores/asterisks also need a word boundary, so ``my_var_name`` and
    ``2 * 3 * 4`` stay as written."""
    s = _escape(s)
    held: list[str] = []

    def _hold(html: str) -> str:
        held.append(html)
        return f"\uE000{len(held) - 1}\uE001"

    def _url(m: re.Match) -> str:
        # A URL ends before trailing emphasis/punctuation: `**https://x**`,
        # `(see https://x).`
        url = m.group(0)
        tail = re.search(r"[*_.,;:!?)\]]+$", url)
        cut = tail.start() if tail else len(url)
        return _hold(url[:cut]) + url[cut:]

    s = re.sub(r"`([^`]+)`", lambda m: _hold(f"<code>{m.group(1)}</code>"), s)
    # Link text keeps its own formatting (``[`file.py`](url)``, ``[**x**](url)``).
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               lambda m: _hold(f'<a href="{m.group(2)}">{_emphasis(m.group(1))}</a>'), s)
    s = re.sub(r"https?://[^\s<]+", _url, s)                    # bare URLs
    s = _emphasis(s)
    # Placeholders can nest (a code span inside link text): restore until none.
    while "\uE000" in s:
        s = re.sub(r"\uE000(\d+)\uE001", lambda m: held[int(m.group(1))], s)
    return s


def _emphasis(s: str) -> str:
    """**bold** / __bold__ / *italic* / _italic_ at word boundaries, hugging
    their text. ``__init__`` stays a name (a dunder is no bold)."""
    s = re.sub(r"\*\*(?=\S)([^*]+?)(?<=\S)\*\*", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<!\w)__(?=\S)([^_]*[^\w_][^_]*?)(?<=\S)__(?!\w)", r"<strong>\1</strong>", s)
    s = re.sub(r"(?<![\w*])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"<em>\1</em>", s)
    return re.sub(r"(?<![\w_])_(?=\S)([^_\n]+?)(?<=\S)_(?![\w_])", r"<em>\1</em>", s)


def inline_fragment(text: str) -> str:
    """A one-line replacement dropped INTO an existing paragraph: storage
    markup passes through; plain text is escaped (entities kept) with only
    `code`, [links](url) and **bold** converted — single-underscore/asterisk
    italics would turn ``my_var_name`` into ``my<em>var</em>name``."""
    if _STORAGE_TAG.search(text):
        return text
    s = _escape(text)
    s = re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", s)
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
               lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)


def _consume_list(lines: list[str], i: int) -> "tuple[str, int]":
    """A run of SAME-TYPE list items starting at ``lines[i]`` → (``<ul>/<ol>``
    html, next index). An ordered run and an unordered run stay separate lists."""
    ordered = bool(re.match(_ORDERED_ITEM, lines[i]))
    item_re = _ORDERED_ITEM if ordered else r"^\s*[-*+]\s+"
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
    hm = re.match(r"^(#{1,6})[ \t]++(.*)$", raw)
    if hm:
        lvl = len(hm.group(1))
        return f"<h{lvl}>{_inline(hm.group(2).strip())}</h{lvl}>", i + 1
    if re.match(r"^\s*[-*+]\s+", lines[i]) or re.match(_ORDERED_ITEM, lines[i]):
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


__all__ = ["inline_fragment", "md_to_storage"]
