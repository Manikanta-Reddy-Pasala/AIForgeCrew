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


def to_jira_wiki(text):
    """Return ``text`` as Jira wiki markup. HTML input → converted; Markdown
    input → converted; plain text → unchanged. ``None``/"" pass through."""
    if not text:
        return text
    return _html_to_wiki(text) if _HTML_HINT.search(text) else _md_to_wiki(text)


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
def _md_to_wiki(s: str) -> str:
    # fenced code ```lang\n…\n``` -> {code:lang}…{code}
    def _fence(m):
        lang = (m.group(1) or "").strip()
        head = "{code:" + lang + "}" if lang else "{code}"
        return "\n" + head + "\n" + m.group(2).rstrip("\n") + _CODE_FENCE
    s = re.sub(r"```([^\n`]*)\n(.*?)```", _fence, s, flags=re.S)

    out_lines = []
    for line in s.split("\n"):
        # atx heading -> hN. (inline-convert the heading text too, so a
        # '## **Bold**' heading doesn't keep its markdown '**')
        m = re.match(r"^(#{1,6})[ \t]+(.*)$", line)
        if m:
            out_lines.append(f"h{len(m.group(1))}. {_md_inline(m.group(2).strip())}")
            continue
        # bullet -   -> *   ;  1.  -> #  — inline-convert the ITEM text so bold
        # ('**API:**'), inline code and links inside a list item render (Jira
        # bold is a SINGLE '*'; markdown '**x**' must become '*x*').
        m = re.match(r"^[ \t]*[-*+][ \t]+(.*)$", line)
        if m:
            out_lines.append("* " + _md_inline(m.group(1)))
            continue
        m = re.match(r"^[ \t]*\d+\.[ \t]+(.*)$", line)
        if m:
            out_lines.append("# " + _md_inline(m.group(1)))
            continue
        out_lines.append(_md_inline(line))
    return _tidy("\n".join(out_lines))


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
    s = re.sub(r"[ \t]++\n", "\n", s)            # trailing spaces
    s = re.sub(r"\n{3,}", "\n\n", s)            # collapse blank runs
    return s.strip()
