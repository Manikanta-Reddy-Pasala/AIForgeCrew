"""Confluence and Jira bodies, READ into markdown.

The write direction already exists (``confluence_format.md_to_storage``,
``jira_format.to_jira_wiki``). The read direction did not: a Confluence page
arrives as storage-format XHTML and a Jira (REST v2) description as wiki
markup, and both were dropped verbatim into markdown dossiers. So a page showed
``<h2>Plan</h2>`` and an issue showed ``h2. Plan`` — and a Jira numbered list
(``# step``) rendered as a stack of top-level HEADINGS.

Dependency-free on purpose (deploy-anywhere), and scoped to the constructs that
real pages and tickets use rather than a full parser. Every pattern here is
bounded or unrolled so a hostile body cannot make it backtrack.
"""
from __future__ import annotations

import html as _html
import re

# ── shared ───────────────────────────────────────────────────────────────────
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_ATX_RE = re.compile(r"^(#{1,6})(\s+\S.*|\s*)$")


def shift_headings(md: str, min_level: int) -> str:
    """Demote every markdown heading so the shallowest one sits at
    ``min_level``, keeping their relative depth (and capping at h6).

    A body pasted under a section heading keeps its own outline: a page whose
    top heading is ``#`` would otherwise sit ABOVE the ``##`` section that
    contains it. Only ever demotes, and never touches ``#`` inside a code fence.
    """
    lines = (md or "").split("\n")
    in_fence = False
    levels: list[int] = []
    for ln in lines:
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
        elif not in_fence and (m := _ATX_RE.match(ln)):
            levels.append(len(m.group(1)))
    if not levels:
        return md or ""
    delta = max(0, min_level - min(levels))
    if delta == 0:
        return md
    out, in_fence = [], False
    for ln in lines:
        if _FENCE_RE.match(ln):
            in_fence = not in_fence
        elif not in_fence and (m := _ATX_RE.match(ln)):
            ln = "#" * min(6, len(m.group(1)) + delta) + m.group(2)
        out.append(ln)
    return "\n".join(out)


def truncate_md(md: str, limit: int) -> str:
    """Cut at a line boundary, and close a code fence the cut left open —
    slicing mid-fence turned the whole rest of a dossier into one code block."""
    if len(md) <= limit:
        return md
    cut = md[:limit]
    nl = cut.rfind("\n")
    if nl > limit // 2:
        cut = cut[:nl]
    if sum(1 for ln in cut.split("\n") if _FENCE_RE.match(ln)) % 2:
        cut += "\n```"
    return cut + "\n\n_… (truncated)_"


# ── Confluence storage XHTML → markdown ──────────────────────────────────────
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
_CODE_MACRO_RE = re.compile(
    r'<ac:structured-macro\b[^>]{0,400}ac:name="(?:code|noformat)"[^>]{0,400}>'
    r"(.*?)</ac:structured-macro>", re.S | re.I)
_LANG_PARAM_RE = re.compile(
    r'<ac:parameter\b[^>]{0,200}ac:name="language"[^>]{0,200}>([^<]{0,40})'
    r"</ac:parameter>", re.I)
_TABLE_RE = re.compile(r"<table\b[^>]{0,400}>(.*?)</table>", re.S | re.I)
_ROW_RE = re.compile(r"<tr\b[^>]{0,400}>(.*?)</tr>", re.S | re.I)
_CELL_RE = re.compile(r"<(t[hd])\b[^>]{0,400}>(.*?)</\1>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]{1,2000}>")


def _stash(held: list[str], block: str) -> str:
    """Park a finished code block behind a placeholder.

    Every later pass — the tag strip above all — would otherwise run over it,
    and code is exactly where ``<ok>`` and ``&amp;`` are literal text: the
    strip deleted a shell redirect, and unescaping rewrote an entity."""
    held.append(block)
    return f"\x00CODE{len(held) - 1}\x00"


def _code_macro(m: re.Match, held: list[str]) -> str:
    inner = m.group(1)
    lang = _LANG_PARAM_RE.search(inner)
    body = _CDATA_RE.search(inner)
    code = body.group(1) if body else _html.unescape(_TAG_RE.sub("", inner))
    fence = f"```{lang.group(1).strip() if lang else ''}\n{code.strip()}\n```"
    return "\n\n" + _stash(held, fence) + "\n\n"


def _cell_text(s: str) -> str:
    """One table cell as a single markdown-safe line."""
    t = _html.unescape(_TAG_RE.sub(" ", s))
    return re.sub(r"\s+", " ", t).strip().replace("|", "\\|")


def _table(m: re.Match) -> str:
    rows = [[_cell_text(c.group(2)) for c in _CELL_RE.finditer(r.group(1))]
            for r in _ROW_RE.finditer(m.group(1))]
    rows = [r for r in rows if r]
    if not rows:
        return "\n"
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    head, *body = rows
    lines = ["| " + " | ".join(head) + " |",
             "|" + "|".join(["---"] * width) + "|"]
    lines += ["| " + " | ".join(r) + " |" for r in body]
    return "\n\n" + "\n".join(lines) + "\n\n"


def storage_to_md(xhtml: str) -> str:
    """Confluence storage-format XHTML → readable markdown.

    Code macros and tables are handled FIRST, as whole units: a code body is
    CDATA that can itself contain ``<`` and ``>``, and a table has to be read
    row by row before the generic tag strip flattens it into run-on text.
    """
    s = xhtml or ""
    held: list[str] = []
    s = _CODE_MACRO_RE.sub(lambda m: _code_macro(m, held), s)
    s = re.sub(r"<pre\b[^>]{0,400}>(.*?)</pre>",
               lambda m: "\n\n" + _stash(
                   held, "```\n" + _html.unescape(m.group(1).strip()) + "\n```") + "\n\n",
               s, flags=re.I | re.S)
    s = _TABLE_RE.sub(_table, s)
    for i in range(6, 0, -1):
        s = re.sub(rf"<h{i}\b[^>]{{0,400}}>(.*?)</h{i}>",
                   lambda m, i=i: "\n\n" + "#" * i + " "
                   + re.sub(r"\s+", " ", _TAG_RE.sub("", m.group(1))).strip() + "\n\n",
                   s, flags=re.I | re.S)
    s = re.sub(r"<code\b[^>]{0,400}>(.*?)</code>", r"`\1`", s, flags=re.I | re.S)
    s = re.sub(r"<(strong|b)\b[^>]{0,400}>(.*?)</\1>", r"**\2**", s, flags=re.I | re.S)
    s = re.sub(r"<(em|i)\b[^>]{0,400}>(.*?)</\1>", r"*\2*", s, flags=re.I | re.S)
    s = re.sub(r'''<a\b[^>]{0,400}href=["']([^"']{1,2000})["'][^>]{0,400}>(.*?)</a>''',
               r"[\2](\1)", s, flags=re.I | re.S)
    s = re.sub(r"<li\b[^>]{0,400}>(.*?)</li>", r"\n- \1", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|ul|ol|blockquote|ac:[\w-]+)>", "\n\n", s, flags=re.I)
    s = _CDATA_RE.sub(lambda m: m.group(1), s)
    s = _TAG_RE.sub("", s)
    s = _html.unescape(s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return re.sub(r"\x00CODE(\d+)\x00", lambda m: held[int(m.group(1))], s)


# ── Jira wiki markup → markdown ──────────────────────────────────────────────
_WIKI_BLOCK_RE = re.compile(r"\{(code|noformat)(?::([^}]{0,200}))?\}(.*?)\{\1\}", re.S)
_WIKI_QUOTE_RE = re.compile(r"\{quote\}(.*?)\{quote\}", re.S)
_WIKI_COLOR_RE = re.compile(r"\{color(?::[^}]{0,40})?\}")
# Marker, whitespace, then text that starts with a NON-space character: the
# whitespace run and the text can never claim the same character, so there is
# exactly one way to split a line.
_WIKI_HEADING_RE = re.compile(r"^h([1-6])\.[ \t]*(\S.*)?$")
_WIKI_OL_RE = re.compile(r"^(#+)[ \t]+(\S.*)$")
_WIKI_UL_RE = re.compile(r"^([*-]+)[ \t]+(\S.*)$")
#: Placeholders that carry a finished code block past every later rewrite.
_FENCE_OPEN = "\x00FENCE\x00"
_FENCE_SEP = "\x00"
_FENCE_CLOSE = "\x00END\x00"
_FENCE_CHUNK_RE = re.compile("(" + re.escape(_FENCE_OPEN) + ".*?"
                             + re.escape(_FENCE_CLOSE) + ")", re.S)
_WIKI_LINK_RE = re.compile(r"\[([^|\]\n]{1,500})\|([^\]\s]{1,2000})\]")
_WIKI_BARE_LINK_RE = re.compile(r"\[((?:https?|mailto):[^\]\s]{1,2000})\]")
_WIKI_IMAGE_RE = re.compile(r"!([^!\s|]{1,300})(?:\|[^!]{0,200})?!")


def _wiki_block(m: re.Match) -> str:
    lang = ""
    for part in (m.group(2) or "").split("|"):
        if part and "=" not in part:
            lang = part.strip()
        elif part.startswith("language="):
            lang = part.split("=", 1)[1].strip()
    # Keep the fenced body out of every later rewrite with a placeholder.
    return _FENCE_OPEN + lang + _FENCE_SEP + m.group(3).strip("\n") + _FENCE_CLOSE


def _wiki_inline(s: str) -> str:
    s = re.sub(r"\{\{([^}\n]{1,500})\}\}", r"`\1`", s)          # {{mono}}
    s = _WIKI_LINK_RE.sub(r"[\1](\2)", s)                       # [text|url]
    s = _WIKI_BARE_LINK_RE.sub(r"\1", s)                        # [url]
    s = _WIKI_IMAGE_RE.sub(r"[image: \1]", s)                   # !img.png!
    # *bold* — only when the stars hug text, so "2 * 3 * 4" is left alone.
    return re.sub(r"(?<![\w*])\*(?=\S)([^*\n]{1,500}?)(?<=\S)\*(?![\w*])",
                  r"**\1**", s)


def _wiki_table_row(ln: str) -> tuple[list[str], bool]:
    header = ln.startswith("||")
    cells = [c.strip() for c in re.split(r"\|\|?", ln.strip().strip("|"))]
    return [_wiki_inline(c).replace("|", "\\|") for c in cells], header


def _wiki_line(ln: str) -> str:
    """One non-table line of wiki markup as markdown."""
    if m := _WIKI_HEADING_RE.match(ln):
        return "#" * int(m.group(1)) + " " + _wiki_inline((m.group(2) or "").strip())
    # A leading `#` is a NUMBERED LIST in Jira. Left alone it is an h1 in
    # markdown — every step of a numbered list rendered as a heading.
    if m := _WIKI_OL_RE.match(ln):
        return "   " * (len(m.group(1)) - 1) + "1. " + _wiki_inline(m.group(2))
    if m := _WIKI_UL_RE.match(ln):
        return "  " * (len(m.group(1)) - 1) + "- " + _wiki_inline(m.group(2))
    if ln.startswith("bq. "):
        return "> " + _wiki_inline(ln[4:])
    if re.fullmatch(r"-{4,}\s*", ln):
        return "---"
    return _wiki_inline(ln)


def _wiki_tables(lines: list[str]) -> list[str]:
    """Consecutive `|`-rows become one markdown table."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        if not lines[i].lstrip().startswith("|"):
            out.append(_wiki_line(lines[i]))
            i += 1
            continue
        rows: list[list[str]] = []
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            rows.append(_wiki_table_row(lines[i].lstrip())[0])
            i += 1
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out.append("| " + " | ".join(rows[0]) + " |")
        out.append("|" + "|".join(["---"] * width) + "|")
        out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return out


def wiki_to_md(text: str) -> str:
    """Jira wiki markup → markdown. Plain text passes through untouched."""
    if not text:
        return text or ""
    s = _WIKI_COLOR_RE.sub("", text.replace("\r\n", "\n"))
    s = _WIKI_BLOCK_RE.sub(_wiki_block, s)
    s = _WIKI_QUOTE_RE.sub(
        lambda m: "\n".join("> " + ln for ln in m.group(1).strip("\n").split("\n")), s)
    out: list[str] = []
    for chunk in _FENCE_CHUNK_RE.split(s):
        if chunk.startswith(_FENCE_OPEN):
            inner = chunk[len(_FENCE_OPEN):-len(_FENCE_CLOSE)]
            lang, _, body = inner.partition(_FENCE_SEP)
            out.append(f"\n```{lang}\n{body}\n```\n")
        else:
            out.append("\n".join(_wiki_tables(chunk.split("\n"))))
    return re.sub(r"\n{3,}", "\n\n", "".join(out)).strip()


__all__ = ["shift_headings", "storage_to_md", "truncate_md", "wiki_to_md"]
