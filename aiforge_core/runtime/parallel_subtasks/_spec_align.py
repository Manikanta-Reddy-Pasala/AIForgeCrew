"""The subtasks that run are the ones the reviewed SPEC lists.

The spec reviewer rewrites SPEC.md — in a live run it narrowed the plan to
``money`` + ``verify`` ("tests are read-only") — but the workers were started
from the architect's list made BEFORE that review (``pyproject``,
``test-money``, ``money``), so the run built what the SPEC ruled out.
:func:`align_to_spec` makes the reviewed SPEC the source of truth: a planned
subtask the SPEC no longer lists is dropped, a listed one keeps the SPEC's
goal, and a SPEC subtask naming a file no worker owns is added. A step with no
file ("verify", "run pytest") is the integration run the pipeline does anyway.
"""
from __future__ import annotations

import re

_SECTION = re.compile(r"^#{2,3}\s*Subtasks\b[^\n]*$", re.I | re.M)
# A subtask line: ``1. <slug> <sep> <goal>``. A slug in backticks or bold is
# taken whole (``src/user-service.ts``); a bare one is one token. The separator
# is a colon, an em/en dash, or a hyphen with spaces around it — never the
# hyphen INSIDE a name: "1. `src/user-service.ts` — add login" once parsed as
# slug ``src/user`` + a stray root ``service.ts``.
_ITEM = re.compile(
    r"^\s*(?:\d{1,3}[.)]|[-*])\s+"
    r"(?:`([^`\n]{1,160})`|\*\*`?([^*`\n]{1,160}?)`?:?\*\*|"
    r"([A-Za-z0-9][\w./-]{0,160}?))"
    r"(?:\s*:\s+|\s*[—–]\s*|\s+-{1,2}\s+)(.+)$")
_PATH = re.compile(r"`([\w./-]{1,160}\.[A-Za-z0-9]{1,8})`|"
                   r"\b([\w-]{1,80}(?:/[\w.-]{1,80}){0,6}\.[A-Za-z]{1,8})\b")
_CODE_EXT = (".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".java", ".kt", ".rs",
             ".rb", ".php", ".cs", ".c", ".cc", ".cpp", ".h", ".hpp", ".swift",
             ".scala", ".sh", ".toml", ".json", ".yaml", ".yml", ".cfg", ".md",
             ".html", ".css", ".sql", ".xml", ".gradle", ".mjs")


def spec_subtasks(spec_md: str) -> list[dict]:
    """``[{slug, goal, paths}]`` from the SPEC's ``## Subtasks`` section."""
    text = str(spec_md or "")
    m = _SECTION.search(text)
    if not m:
        return []
    body = text[m.end():]
    nxt = re.search(r"^#{1,3}\s", body, re.M)
    body = body[:nxt.start()] if nxt else body
    out = []
    for line in body.splitlines():
        it = _ITEM.match(line)
        if not it:
            continue
        slug = (it.group(1) or it.group(2) or it.group(3) or "").strip()
        slug = slug.strip("`").rstrip(":").strip()
        goal = it.group(4).strip()
        paths = []
        for a, b in _PATH.findall(goal):
            p = (a or b).strip().lstrip("./")
            if p.endswith(_CODE_EXT) and not p.startswith(("http", "www.")) \
                    and p not in paths:
                paths.append(p)
        if slug.endswith(_CODE_EXT) and slug not in paths:
            paths.insert(0, slug)
        out.append({"slug": slug, "goal": goal, "paths": paths})
    return out


def _slug_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def _stem(path: str) -> str:
    return _slug_key(str(path or "").rsplit("/", 1)[-1].rsplit(".", 1)[0])


def _match(item: dict, subs: list, used: set) -> int:
    key = _slug_key(item["slug"])
    for i, s in enumerate(subs):
        if i in used:
            continue
        p = str(s.get("path") or "").lstrip("./")
        if p and p in item["paths"]:
            return i
    for i, s in enumerate(subs):
        if i in used:
            continue
        if key and key in (_slug_key(s.get("slug")), _stem(s.get("path"))):
            return i
    return -1


def align_to_spec(subs: list, spec_md: str) -> tuple[list, list, list]:
    """``(subs, dropped_slugs, added_slugs)``. Unchanged when the SPEC lists
    no subtasks, or when aligning would leave nothing to build."""
    items = spec_subtasks(spec_md)
    if not items or not subs:
        return subs, [], []
    used: set = set()
    out, added = [], []
    for it in items:
        i = _match(it, subs, used)
        if i >= 0:
            used.add(i)
            s = dict(subs[i])
            path = s.get("path") or ""
            s["goal"] = (f"{path}: {it['goal']}" if path and path not in it["goal"]
                         else it["goal"])
            out.append(s)
        elif it["paths"]:
            path = it["paths"][0]
            if any((o.get("path") or "") == path for o in out):
                continue
            out.append({"slug": _slug_key(it["slug"]) or _stem(path),
                        "path": path, "goal": f"{path}: {it['goal']}"})
            added.append(out[-1]["slug"])
    if not out:
        return subs, [], []
    dropped = [s.get("slug") or s.get("path") for i, s in enumerate(subs)
               if i not in used]
    return out, dropped, added


def _subtask_section(text: str) -> tuple[int, int] | None:
    """``(start, end)`` of the ``## Subtasks`` section (heading included)."""
    m = _SECTION.search(text)
    if not m:
        return None
    nxt = re.search(r"^#{1,3}\s", text[m.end():], re.M)
    return m.start(), (m.end() + nxt.start()) if nxt else len(text)


def looks_truncated(original: str, reviewed: str, cut: bool = False) -> bool:
    """A reviewed SPEC that stopped early: the reply hit its token budget
    (``cut``), a code fence is left open, or the original has sections after
    ``## Subtasks`` and the rewrite ends inside that section."""
    if cut or reviewed.count("```") % 2:
        return True
    o, r = _subtask_section(original), _subtask_section(reviewed)
    if o is None:
        return False
    if r is None:
        return True
    return o[1] < len(original.rstrip()) and r[1] >= len(reviewed.rstrip())


def keep_planned_subtasks(original: str, reviewed: str,
                          cut: bool = False) -> tuple[str, bool]:
    """The reviewed SPEC, with the original ``## Subtasks`` section (and the
    sections after it) put back when the rewrite was cut short and lists
    fewer subtasks — a reply that ran out of tokens mid-list must not drop
    the subtasks it never reached. A complete rewrite that narrows the list
    on purpose is kept. ``(spec, restored)``."""
    before = spec_subtasks(original)
    if not before or not looks_truncated(original, reviewed, cut):
        return reviewed, False
    if len(spec_subtasks(reviewed)) >= len(before) and not cut:
        return reviewed, False
    o = _subtask_section(original)
    r = _subtask_section(reviewed)
    head = reviewed[:r[0]] if r else reviewed.rstrip() + "\n\n"
    if head.count("```") % 2:
        head = head.rstrip() + "\n```\n\n"
    return head + original[o[0]:], True


__all__ = ["align_to_spec", "keep_planned_subtasks", "looks_truncated",
           "spec_subtasks"]
