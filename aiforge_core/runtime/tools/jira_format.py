"""Convert an agent-authored comment / description into Jira **wiki markup**.

Jira Server/DC (REST v2, what jira.py targets) renders comment and description
bodies as wiki markup — NOT HTML and NOT Markdown. The model naturally emits one
of those, so a raw `<p><strong>…` or `## …` body shows up with the literal tags.
:func:`to_jira_wiki` normalizes either into wiki markup so it renders correctly.

Deliberately dependency-free (deploy-anywhere): a small, targeted converter for
the constructs agents actually produce, not a full HTML/Markdown parser.
"""
from __future__ import annotations

import html as _html
import re

_CODE_FENCE = "\n{code}\n"

_HTML_HINT = re.compile(r"</?(p|br|ul|ol|li|strong|b|em|i|code|pre|h[1-6]|a|div)\b",
                        re.I)


# Already wiki markup: headings "h2. ", tables "||", macros "{code}"/"{panel}",
# links to a URL/user/anchor/issue "[text|https://…]", nested bullets "** ".
# Such text came from the issue itself (the model edited what jira_read
# returned) — "converting" it again turned every numbered item ("# step")
# into an "h1." heading. Code spans and fences are ignored when looking, so a
# `list[int | None]` or a `{note}` placeholder does not make Markdown "wiki".
_WIKI_HINT = re.compile(
    r"^[ \t]*h[1-6]\.[ \t]|^[ \t]*\|\||^[ \t]*\*\*+[ \t]"
    r"|\{(code|noformat|panel|quote|color|info|note|warning|tip)(:[^}\n]*)?\}"
    r"|\[[^\]\n|]+\|(https?://|mailto:|~|#|[A-Z][A-Z0-9]+-\d+)[^\]\n]*\]",
    re.M)
_MD_CODE = re.compile(r"```.*?```|`[^`\n]+`", re.S)


def _looks_like_wiki(text: str) -> bool:
    return bool(_WIKI_HINT.search(_MD_CODE.sub(" ", text)))


def to_jira_wiki(text, *, hash_is_list: bool = False):
    """Return ``text`` as Jira wiki markup. HTML input → converted; Markdown
    input → converted; text that is already wiki markup, or plain text →
    unchanged. ``None``/"" pass through. ``hash_is_list``: every "# x" line is
    a numbered item (the target issue numbers its steps that way), never an
    H1."""
    if not text:
        return text
    if _HTML_HINT.search(text):
        return _html_to_wiki(text)
    if _looks_like_wiki(text):
        return text
    return _md_to_wiki(text, hash_is_list=hash_is_list)


# ── HTML → wiki ──────────────────────────────────────────────────────────────
def _html_to_wiki(s: str) -> str:
    # code first, so tag-stripping never touches code contents
    s = re.sub(r"<pre\b[^>]*>(.*?)</pre>", lambda m: _CODE_FENCE
               + _strip_tags(m.group(1)).strip("\n") + _CODE_FENCE,
               s, flags=re.I | re.S)
    s = re.sub(r"<code\b[^>]*>(.*?)</code>",
               lambda m: "{{" + _strip_tags(m.group(1)) + "}}", s, flags=re.I | re.S)
    # headings
    s = re.sub(r"<h([1-6])\b[^>]*>(.*?)</h\1>",
               lambda m: f"\nh{m.group(1)}. " + _strip_tags(m.group(2)).strip() + "\n",
               s, flags=re.I | re.S)
    # bold / italic
    s = re.sub(r"</?(strong|b)\b[^>]*>", "*", s, flags=re.I)
    s = re.sub(r"</?(em|i)\b[^>]*>", "_", s, flags=re.I)
    # links: <a href="u">t</a> -> [t|u]
    s = re.sub(r'<a\b[^>]*\bhref\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
               lambda m: f"[{_strip_tags(m.group(2)).strip()}|{m.group(1)}]",
               s, flags=re.I | re.S)
    # lists: ordered items -> '# ', unordered -> '* '
    s = re.sub(r"<ol\b[^>]*>(.*?)</ol>",
               lambda m: "\n" + _list_items(m.group(1), "#") + "\n", s, flags=re.I | re.S)
    s = re.sub(r"<ul\b[^>]*>(.*?)</ul>",
               lambda m: "\n" + _list_items(m.group(1), "*") + "\n", s, flags=re.I | re.S)
    # any stray <li> outside a list -> bullet
    s = re.sub(r"<li\b[^>]*>(.*?)</li>",
               lambda m: "* " + _strip_tags(m.group(1)).strip() + "\n", s, flags=re.I | re.S)
    # paragraphs / breaks / divs -> newlines
    s = re.sub(r"</p\s*>", "\n\n", s, flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</div\s*>", "\n", s, flags=re.I)
    s = _strip_tags(s)                          # drop <p>, <div>, <ul>… leftovers
    s = _html.unescape(s)
    return _tidy(s)


def _list_items(inner: str, bullet: str) -> str:
    items = re.findall(r"<li\b[^>]*>(.*?)</li>", inner, flags=re.I | re.S)
    return "\n".join(f"{bullet} " + _strip_tags(it).strip() for it in items)


def _strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


# ── Markdown → wiki ──────────────────────────────────────────────────────────
def _md_to_wiki(s: str, *, hash_is_list: bool = False) -> str:
    # fenced code ```lang\n…\n``` -> {code:lang}…{code}
    def _fence(m):
        lang = (m.group(1) or "").strip()
        head = "{code:" + lang + "}" if lang else "{code}"
        return "\n" + head + "\n" + m.group(2).rstrip("\n") + _CODE_FENCE
    s = re.sub(r"```([^\n`]*)\n(.*?)```", _fence, s, flags=re.S)

    out_lines = []
    lines = s.split("\n")
    for i, line in enumerate(lines):
        # A RUN of "# item" lines is a Jira numbered list, not a stack of
        # markdown H1s — keep it (a lone "# Title" is still a heading).
        if _one_hash(line) and (hash_is_list or _in_hash_list(lines, i)):
            out_lines.append(line)
            continue
        # atx heading -> hN. (inline-convert the heading text too, so a
        # '## **Bold**' heading doesn't keep its markdown '**')
        m = re.match(r"^(#{1,6})[ \t]++(.*)$", line)
        if m:
            out_lines.append(f"h{len(m.group(1))}. {_md_inline(m.group(2).strip())}")
            continue
        # bullet -   -> *   ;  1.  -> #  — inline-convert the ITEM text so bold
        # ('**API:**'), inline code and links inside a list item render (Jira
        # bold is a SINGLE '*'; markdown '**x**' must become '*x*').
        m = re.match(r"^[ \t]*+[-*+][ \t]++(.*)$", line)
        if m:
            out_lines.append("* " + _md_inline(m.group(1)))
            continue
        m = re.match(r"^[ \t]*+\d++\.[ \t]++(.*)$", line)
        if m:
            out_lines.append("# " + _md_inline(m.group(1)))
            continue
        out_lines.append(_md_inline(line))
    return _tidy("\n".join(out_lines))


def _one_hash(line: str) -> bool:
    return bool(re.match(r"^[ \t]*#[ \t]+\S", line))


def _in_hash_list(lines: list[str], i: int) -> bool:
    return _one_hash(lines[i]) and (
        (i > 0 and _one_hash(lines[i - 1]))
        or (i + 1 < len(lines) and _one_hash(lines[i + 1])))


def _md_inline(line: str) -> str:
    # inline code `x` -> {{x}} (guard bold/italic from touching its contents)
    line = re.sub(r"`([^`]+)`", lambda m: "{{" + m.group(1) + "}}", line)
    # links [t](u) -> [t|u]
    line = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)",
                  lambda m: f"[{m.group(1)}|{m.group(2)}]", line)
    # bold **x** / __x__ -> *x*
    line = re.sub(r"\*\*([^*]+)\*\*", r"*\1*", line)
    line = re.sub(r"__([^_]+)__", r"*\1*", line)
    return line


def _tidy(s: str) -> str:
    # Trailing spaces, line by line: `[ \t]+\n` is a quantifier over
    # user-controlled text (a scanner flags it as a DoS risk) and rstrip does
    # the same job in one pass with nothing to backtrack.
    s = "\n".join(line.rstrip(" \t") for line in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s)            # collapse blank runs
    return s.strip()
