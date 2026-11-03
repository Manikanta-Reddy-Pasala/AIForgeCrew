"""Find the text an edit means to replace, forgiving the slips a model makes.

An exact ``body.count(old)`` refused an edit for a stray space, a ``12|``
line-number prefix copied from a read, or a block written one indent level
off — and every refusal cost a round trip (re-read, retry, often a switch to
writing the file through the shell). OpenCode tries a chain of matchers
before it gives up; this is the same idea, kept conservative, because an
edit in the WRONG place is far worse than a miss:

1. exact text;
2. the text without read-tool line-number prefixes — only ``N|`` / ``N│``
   prefixes whose numbers run in sequence (``1: "a"`` in a dict or ``10: x``
   in YAML is content, never a prefix);
3. the same lines compared trimmed, and only when every line's indent
   relative to the first is the same in the file as in ``old`` — the new
   text is then shifted line by line to the file's indent;
4. the same lines with runs of whitespace collapsed (same indent rule);
5. a block anchored on its first and last lines (at least 4 lines, the last
   at the same indent relative to the first as in ``old``, the middle at
   least 75% alike — 85% when short).

Every stage needs exactly ONE match; two or more is "ambiguous", never a
guess. A miss returns the file's closest lines (numbered), so the next try
is right. Files are handled with "\\n" line ends (the callers read text
mode). Off switch: ``AIFORGE_EDIT_FUZZY=0`` (exact only).
"""
from __future__ import annotations

import difflib
import os
import re

# The read tool prints "12|<line>" (nothing after the bar): the line's own
# indent starts right after it.
_PREFIX = re.compile(r"^\s*(\d+)[|│]")
_HINT_LINES = 8
#: A file this long gets no closest-lines hint (the search is per line).
_HINT_MAX_LINES = 20000


def _fuzzy_on() -> bool:
    return os.environ.get("AIFORGE_EDIT_FUZZY", "1").strip().lower() not in ("0", "false", "no", "off")


def _lines(text: str) -> list:
    """Lines with their "\\n" kept — split on "\\n" only (``splitlines``
    also splits on \\x0c, \\x85, \\u2028 inside a line)."""
    return [p for p in re.split(r"(?<=\n)", text) if p]


def _strip_prefixes(text: str, need_sequence: bool) -> "str | None":
    """``text`` without ``N|`` prefixes, or None when it does not carry them
    on every non-empty line (in sequence, when ``need_sequence``)."""
    rows = text.split("\n")
    nums = []
    for ln in rows:
        if not ln.strip():
            continue
        m = _PREFIX.match(ln)
        if not m:
            return None
        nums.append(int(m.group(1)))
    if not nums:
        return None
    if need_sequence and any(b != a + 1 for a, b in zip(nums, nums[1:])):
        return None
    return "\n".join(_PREFIX.sub("", ln, count=1) if ln.strip() else ln for ln in rows)


def _indent(s: str) -> str:
    return s[:len(s) - len(s.lstrip(" \t"))]


def _shift(want: list, got: list) -> "tuple[str, str] | None":
    """``(old_base, file_base)`` when every non-empty line of ``want`` (old)
    sits at the same indent relative to its first line as the matched file
    lines ``got`` do; None when the shapes differ (a flat ``b: 1`` under
    ``a:`` is not the same block as an indented one)."""
    pairs = [(w, g) for w, g in zip(want, got) if w.strip()]
    if not pairs:
        return None
    ob, fb = _indent(pairs[0][0]), _indent(pairs[0][1])
    for w, g in pairs:
        wi, gi = _indent(w), _indent(g)
        if not wi.startswith(ob) or gi != fb + wi[len(ob):]:
            return None
    return ob, fb


def _reindent(new: str, ob: str, fb: str) -> "str | None":
    """``new`` moved from the old base indent to the file's, line by line;
    None when a line of ``new`` sits left of the base (no safe mapping)."""
    if ob == fb:
        return new
    out = []
    for ln in new.split("\n"):
        if not ln.strip():
            out.append(ln)
        elif ln.startswith(ob):
            out.append(fb + ln[len(ob):])
        else:
            return None
    return "\n".join(out)


def _splice(body_lines: list, i: int, n: int, new: str) -> str:
    """``body_lines[i:i+n]`` replaced by ``new`` (its line end kept; an empty
    ``new`` removes the lines whole, no blank line left)."""
    if new and body_lines[i + n - 1].endswith("\n") and not new.endswith("\n"):
        new += "\n"
    return "".join(body_lines[:i]) + new + "".join(body_lines[i + n:])


def _line_match(body: str, old: str, new: str, same):
    keep = _lines(body)
    file_lines = [ln.rstrip("\n") for ln in keep]
    want = old.strip("\n").split("\n")
    if not any(w.strip() for w in want):
        return None
    n = len(want)
    hits = [i for i in range(len(file_lines) - n + 1)
            if all(same(file_lines[i + k], want[k]) for k in range(n))]
    if len(hits) > 1:
        return ("ambiguous", len(hits))
    if not hits:
        return None
    i = hits[0]
    shape = _shift(want, file_lines[i:i + n])
    if shape is None:
        return ("indent", i + 1)        # the lines are there, the nesting is not
    moved = _reindent(new, *shape)
    if moved is None:
        return ("indent", i + 1)
    return _splice(keep, i, n, moved)


def _anchor_match(body: str, old: str, new: str):
    keep = _lines(body)
    raw = [ln.rstrip("\n") for ln in keep]
    trimmed = [ln.strip() for ln in raw]
    want_raw = old.strip("\n").split("\n")
    want = [w.strip() for w in want_raw]
    n = len(want)
    if n < 4 or not want[0] or not want[-1]:
        return None
    need = 0.85 if n - 2 < 3 else 0.75
    middle = "\n".join(want[1:-1])
    first_ind, last_ind = _indent(want_raw[0]), _indent(want_raw[-1])
    if not last_ind.startswith(first_ind):
        return None
    rel = last_ind[len(first_ind):]      # the last line's indent past the first
    scored = []
    for i, ln in enumerate(trimmed):
        if ln != want[0]:
            continue
        # the closing line sits where old puts it relative to the opening one
        # (an inner `}` or `return` deeper in does not end the block)
        ind = _indent(raw[i]) + rel
        for j in range(i + max(3, int(n * 0.8)) - 1, min(len(trimmed), i + int(n * 1.2) + 1)):
            if trimmed[j] == want[-1] and _indent(raw[j]) == ind:
                sm = difflib.SequenceMatcher(None, "\n".join(trimmed[i + 1:j]), middle)
                if sm.real_quick_ratio() >= need - 0.15:
                    scored.append((sm.ratio(), i, j))
                break
    hits = [x for x in scored if x[0] >= need]
    # a sibling block nearly as alike: no telling which one was meant
    if len(hits) > 1 or (hits and any(x is not hits[0] and x[0] >= need - 0.15 for x in scored)):
        return ("ambiguous", len(scored))
    if not hits:
        return None
    _, i, j = hits[0]
    moved = _reindent(new, _indent(want_raw[0]), _indent(raw[i]))
    if moved is None:
        return None
    return _splice(keep, i, j - i + 1, moved)


def _closest(body: str, old: str) -> list:
    """The file's lines nearest the first line of ``old``, numbered."""
    lines = body.split("\n")
    head = next((w.strip() for w in old.split("\n") if w.strip()), "")
    if not head or not lines or len(lines) > _HINT_MAX_LINES:
        return []
    stripped = [ln.strip() for ln in lines]
    best = difflib.get_close_matches(head, stripped, n=1, cutoff=0.5)
    if not best:
        return []
    i = stripped.index(best[0])
    span = max(3, len(old.split("\n")))
    lo, hi = max(0, i - 1), min(len(lines), i + min(span, _HINT_LINES))
    return [f"{k + 1}|{lines[k]}" for k in range(lo, hi)]


def _miss(body: str, old: str) -> dict:
    return {"ok": False, "error": "old_text_not_found", "closest": _closest(body, old),
            "hint": "old_text must match the file; the closest lines are above "
                    "(the numbers are line numbers — do not copy them into old_text)"}


def _fuzzy(body: str, old: str, new: str) -> "dict | None":
    for same, how in ((lambda a, b: a.strip() == b.strip(), "lines_trimmed"),
                      (lambda a, b: " ".join(a.split()) == " ".join(b.split()),
                       "whitespace_normalized")):
        hit = _line_match(body, old, new, same)
        if isinstance(hit, tuple) and hit[0] == "indent":
            # Found, but nested differently (or new sits left of it): never
            # an anchor guess past that — say what is wrong instead.
            return {"ok": False, "error": "indent_mismatch", "line": hit[1],
                    "hint": f"these lines are at line {hit[1]}, but with a different "
                            "indentation than old_text has; copy old_text with the "
                            "file's own indentation"}
        if isinstance(hit, tuple):
            return {"ok": False, "error": "ambiguous_match", "occurrences": hit[1]}
        if hit is not None:
            return {"ok": True, "how": how, "body": hit}
    hit = _anchor_match(body, old, new)
    if isinstance(hit, tuple):
        return {"ok": False, "error": "ambiguous_match", "occurrences": hit[1]}
    if hit is not None:
        return {"ok": True, "how": "block_anchor", "body": hit}
    return None


def replace_once(body: str, old: str, new: str, replace_all: bool = False) -> dict:
    """``{"ok": True, "body", "how"}`` or ``{"ok": False, "error", ...}``
    (``old_text_not_found`` with ``closest`` lines, ``ambiguous_match`` with
    ``occurrences``, ``empty_old_text``)."""
    if not old:
        return {"ok": False, "error": "empty_old_text"}
    n = body.count(old)
    if n == 1 or (n > 1 and replace_all):
        return {"ok": True, "how": "exact",
                "body": body.replace(old, new) if replace_all else body.replace(old, new, 1)}
    if n > 1:
        return {"ok": False, "error": "ambiguous_match", "occurrences": n}
    if not _fuzzy_on() or replace_all:
        return _miss(body, old)
    bare = _strip_prefixes(old, need_sequence=True)
    if bare is not None:
        if any(_PREFIX.match(ln) for ln in new.split("\n") if ln.strip()):
            new_bare = _strip_prefixes(new, need_sequence=False)
            if new_bare is None:
                return _miss(body, old)      # prefixed on some lines only: unsafe
        else:
            new_bare = new
        # Prefixed text was whole lines: it matches whole lines only (a bare
        # "    y = 2" must not take the start of "    y = 20").
        at = [m.start() for m in re.finditer(re.escape(bare), body)
              if (m.start() == 0 or body[m.start() - 1] == "\n")
              and (m.end() == len(body) or body[m.end()] == "\n" or bare.endswith("\n"))]
        if len(at) == 1:
            return {"ok": True, "how": "line_numbers_removed",
                    "body": body[:at[0]] + new_bare + body[at[0] + len(bare):]}
        if len(at) > 1:
            return {"ok": False, "error": "ambiguous_match", "occurrences": len(at)}
        hit = _fuzzy(body, bare, new_bare)
        if hit is not None:
            return hit
    hit = _fuzzy(body, old, new)
    return hit if hit is not None else _miss(body, old)


__all__ = ["replace_once"]
