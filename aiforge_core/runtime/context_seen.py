"""Skip a repeated skill, workflow, OKF page, or memory hit.

A later copy is omitted only when this turn's messages already contain the
same identity (name, path, or source) AND the same body. Equality is the
body text itself; ``content_hash`` is the stable digest of that text, so a
changed page, a different skill, or a newer memory hit still goes through.
The first copy is never dropped — nothing is omitted unless an earlier
message already holds it.

Short fragments are always sent. A 27B model should not lose a fact because
a few words happened to sit near an id.
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re

# How far from the body the identity may sit (a heading, a JSON path, or a
# ``(source)`` suffix). Long enough for a one-line description between a
# skill name and its body; short of "the word appears somewhere in the turn".
_BEFORE = 2000
_AFTER = 400
# Below this, the body is too small to prove it is the same document.
_MIN_BODY = 40

_READ_TOOLS = frozenset({"file_read", "read_files", "read_lines"})

_RECALL_LINE = re.compile(r"^- (?P<text>.+?)  \((?P<source>[^)]+)\)\s*$")
_OKF_HEAD = re.compile(
    r"^(PROJECT MEMORY \([^)\n]+\)|LINKED MEMORY \([^)\n]+\)|GLOBAL MEMORY):\n",
)
_FM = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*\n?(.*)$", re.DOTALL)
_READ_HEAD = re.compile(r"^=== (.+?) ===$")


# Bodies this turn has already sent, so compaction can put one back if it
# drops the copy a pointer names. Scoped to the turn (reset at the start of
# run_chat_agent). Not a process-wide cache: another chat must not expand
# a pointer into this chat's text, and a cap must not forget a live pointer.
_SEEN_BODIES: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "aiforge_seen_bodies", default=None)
_POINTER_RE = re.compile(
    r"\[already in context: [^\]]*sha256:([0-9a-f]{16}) — "
    r"same text is above; not repeated\]")


def reset_seen_bodies() -> None:
    """Drop the previous turn's bodies. Call once at the start of a turn."""
    _SEEN_BODIES.set({})


def _bag() -> dict[str, str]:
    bag = _SEEN_BODIES.get()
    if bag is None:
        bag = {}
        _SEEN_BODIES.set(bag)
    return bag


def _remember_body(body_n: str) -> str:
    digest = hashlib.sha256(body_n.encode("utf-8")).hexdigest()[:16]
    _bag()[digest] = body_n
    return digest


def content_hash(body: str) -> str:
    """Stable digest of a body. Trailing whitespace and CRLF do not change it;
    any other edit does."""
    return _remember_body(_norm(body))


def pointer(kind: str, ident: str, body: str) -> str:
    """One line standing in for a body that is already in the turn."""
    return (f"[already in context: {kind} {ident} "
            f"sha256:{content_hash(body)} — same text is above; not repeated]")


def already_in_context(messages, *, kind: str, ident: str, body: str) -> bool:
    """True when ``messages`` already hold this exact body next to ``ident``.

    ``kind`` selects the artifact family (skill, workflow, okf, memory). It
    is part of the contract callers pass; the match itself is identity plus
    body, which is what distinguishes a changed page from a repeat.
    """
    del kind  # the identity string is what the messages actually contain
    ident_n = (ident or "").strip()
    body_n = _norm(body)
    if len(ident_n) < 2 or len(body_n) < _MIN_BODY:
        return False
    # Tool calls store the body as JSON, so newlines are escaped. A write the
    # model just made is the same page as the read that follows it.
    forms = [body_n]
    escaped = json.dumps(body_n)[1:-1]
    if escaped != body_n:
        forms.append(escaped)
    for text in _texts(messages):
        if any(_near(text, ident_n, form) for form in forms):
            return True
    return False


def shrink_block(messages, kind: str, block: str) -> str:
    """Drop repeated bodies out of one injected block. The preamble and every
    first copy stay. Unknown shapes are returned unchanged."""
    if not block or not messages:
        return block
    try:
        if kind in ("skill", "workflow"):
            return _shrink_playbook(messages, kind, block)
        if kind == "memory":
            return _shrink_recall(messages, block)
        if kind == "okf":
            return _shrink_okf(messages, block)
    except Exception:  # noqa: BLE001 — context must never break a turn
        return block
    return block


def dedupe_tool_result(messages, name: str, args, result, cwd: str | None = None):
    """A copy of ``result`` with repeated artifact bodies replaced by pointers.

    The caller's object is not modified (the UI still shows the raw tool
    result). Anything that is not an OKF page, skill, workflow, or memory
    hit is returned as-is, including ordinary source-file reads.
    """
    if not isinstance(result, dict) or result.get("ok") is False:
        return result
    try:
        if name in ("skill_search", "workflow_search"):
            return _dedupe_playbook_hits(messages, name, result, cwd)
        if name in ("memory_lookup", "memory_write"):
            return _dedupe_memory(messages, name, result)
        if name in _READ_TOOLS:
            return _dedupe_read(messages, name, args if isinstance(args, dict) else {},
                                result)
    except Exception:  # noqa: BLE001
        return result
    return result


def carries_fresh_body(name: str, result) -> bool:
    """True when a skill or workflow search attached a new body.

    Those bodies have to survive the 6k observation slice. Memory hits are
    already short (a few hundred characters) and stay on the normal cap.
    """
    if name not in ("skill_search", "workflow_search") or not isinstance(result, dict):
        return False
    for key in ("skills", "workflows", "hits"):
        for hit in result.get(key) or []:
            if not isinstance(hit, dict) or hit.get("already_in_context"):
                continue
            body = str(hit.get("body") or hit.get("text") or "")
            if len(body) >= _MIN_BODY and not body.startswith("[already in context:"):
                return True
    return False


# ── matching ────────────────────────────────────────────────────────────


def _norm(body: str) -> str:
    return (body or "").replace("\r\n", "\n").strip()


def _texts(messages) -> list[str]:
    out: list[str] = []
    for m in messages or []:
        if isinstance(m, str):
            if m:
                out.append(m)
            continue
        if not isinstance(m, dict):
            continue
        c = m.get("content")
        if isinstance(c, str):
            if c:
                out.append(c)
        elif isinstance(c, list):
            bits = [str(p.get("text") or "") for p in c
                    if isinstance(p, dict) and p.get("type") == "text"]
            joined = "\n".join(b for b in bits if b)
            if joined:
                out.append(joined)
    return out


def _near(text: str, ident: str, body: str) -> bool:
    """The identity sits just outside this body, and the body is the whole
    document — not a prefix of a longer page that has since been edited."""
    text_n = text.replace("\r\n", "\n")
    start = 0
    while True:
        i = text_n.find(body, start)
        if i < 0:
            return False
        end = i + len(body)
        lo = max(0, i - _BEFORE)
        hi = min(len(text_n), end + _AFTER)
        before = text_n[lo:i]
        after = text_n[end:hi]
        # The identity has to label THIS copy. A name that only occurs inside
        # the body must not make a different skill with the same text look
        # like a repeat.
        if (ident in before or ident in after) and _ends_document(text_n, end):
            return True
        start = i + max(1, len(body))


def _ends_document(text: str, end: int) -> bool:
    """True when ``end`` is the end of the stored document, not mid-sentence.

    A shortened page is a prefix of the old text. That must not count: the
    model would keep the stale longer copy and never see the deletion.
    """
    n = len(text)
    # Tool JSON stores a trailing newline as the two characters \ n.
    while end < n and text.startswith("\\n", end):
        end += 2
    if end >= n:
        return True
    rest = text[end:]
    if rest[0] in "\"'`":
        return True
    if rest.startswith("\n"):
        nxt = rest[1:]
        if nxt == "" or nxt[0] == "\n" or nxt.startswith("### ") or nxt.startswith("["):
            return True
    if rest.startswith("  ("):
        return True
    return False


def _body_present(blob: str, body: str) -> bool:
    if body in blob:
        return True
    escaped = json.dumps(body)[1:-1]
    return escaped != body and escaped in blob


def restore_dangling(messages: list, dropped: list | None = None) -> list:
    """Put a body back when compaction dropped the copy its pointer names.

    The system prompt and the recent tail are kept. A pointer is only safe
    while that text is still in one of them. The first dangling pointer for a
    body is expanded; a later one stays a pointer once that body is back.
    ``dropped`` is the condensed middle, searched so a pointer from an earlier
    step can be restored without a process-wide cache.
    """
    if not messages:
        return messages
    kept = "\n".join(_texts(messages))
    needed = set(_POINTER_RE.findall(kept))
    if not needed:
        return messages
    found = _bodies_for(needed, _texts(dropped or []))
    blob = _POINTER_RE.sub(" ", kept)
    changed = False
    out = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, str) or "already in context:" not in content:
            out.append(m)
            continue
        new, blob = _expand_once(content, blob, found)
        if new != content:
            changed = True
            out.append({**m, "content": new})
        else:
            out.append(m)
    return out if changed else messages


def _bodies_for(needed: set[str], texts: list[str]) -> dict[str, str]:
    bag = _SEEN_BODIES.get() or {}
    found = {h: bag[h] for h in needed if h in bag}
    missing = needed - found.keys()
    for text in texts:
        if not missing:
            break
        for cand in _candidate_bodies(text):
            digest = hashlib.sha256(cand.encode("utf-8")).hexdigest()[:16]
            if digest in missing:
                found[digest] = cand
                missing.discard(digest)
                if not missing:
                    break
    return found


def _expand_once(content: str, blob: str, found: dict[str, str]) -> tuple[str, str]:
    def _repl(match):
        nonlocal blob
        body = found.get(match.group(1), "")
        if not body or _body_present(blob, body):
            return match.group(0)
        blob = blob + "\n" + body
        return body

    return _POINTER_RE.sub(_repl, content), blob


def _candidate_bodies(text: str):
    """Document-shaped slices of a dropped message. Used to match a pointer's
    digest back to the page compaction is about to discard."""
    n = _norm(text)
    if len(n) >= _MIN_BODY:
        yield n
    _pre, sections = _playbook_sections(text)
    for chunk in sections:
        _head, _, body = chunk.partition("\n")
        body_s = body.strip()
        if len(body_s) >= _MIN_BODY:
            yield body_s
    for ln in text.split("\n"):
        m = _RECALL_LINE.match(ln)
        if not m:
            continue
        t = m.group("text").strip()
        if len(t) >= _MIN_BODY:
            yield t
    marker = "MEMORY (prior facts"
    i = text.find(marker)
    if i >= 0:
        nl = text.find("\n", i)
        if nl >= 0:
            tail = _norm(text[nl + 1:])
            if len(tail) >= _MIN_BODY:
                yield tail
    start = 0
    decoder = json.JSONDecoder()
    while True:
        j = text.find("{", start)
        if j < 0:
            break
        try:
            obj, end = decoder.raw_decode(text[j:])
        except json.JSONDecodeError:
            start = j + 1
            continue
        yield from _json_strings(obj)
        start = j + end


def _json_strings(obj):
    if isinstance(obj, str):
        n = _norm(obj)
        if len(n) >= _MIN_BODY:
            yield n
            _meta, body = _frontmatter(n)
            body_s = (body or "").strip()
            if len(body_s) >= _MIN_BODY and body_s != n:
                yield body_s
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _json_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _json_strings(v)


def _known(messages, kind: str, idents: list[str], body: str) -> bool:
    for ident in idents:
        if ident and already_in_context(messages, kind=kind, ident=ident, body=body):
            return True
    return False


# ── injected blocks ─────────────────────────────────────────────────────


def _section_name(head: str) -> str:
    s = head.strip()
    if s.startswith("### "):
        s = s[4:]
    return s.split(" — ", 1)[0].strip()


def _shrink_playbook(messages, kind: str, block: str) -> str:
    preamble, sections = _playbook_sections(block)
    if not sections:
        return block
    running: list = list(messages)
    acc = preamble
    rendered: list[str] = []
    for chunk in sections:
        head, _, body = chunk.partition("\n")
        name = _section_name(head)
        body_s = body.strip()
        if name and _known(running, kind, [name], body_s):
            chunk = head + "\n" + pointer(kind, name, body_s)
        rendered.append(chunk)
        acc = (acc + "\n" + chunk).strip()
        running = [*messages, {"role": "system", "content": acc}]
    body_out = "\n".join(rendered)
    if preamble:
        return preamble.rstrip() + "\n" + body_out
    return body_out


def _playbook_sections(block: str) -> tuple[str, list[str]]:
    preamble: list[str] = []
    sections: list[list[str]] = []
    cur: list[str] | None = None
    for ln in block.split("\n"):
        if ln.startswith("### "):
            if cur is not None:
                sections.append(cur)
            cur = [ln]
        elif cur is None:
            preamble.append(ln)
        else:
            cur.append(ln)
    if cur is not None:
        sections.append(cur)
    return "\n".join(preamble).strip(), ["\n".join(s) for s in sections]


def _shrink_recall(messages, block: str) -> str:
    running = ""
    out: list[str] = []
    changed = False
    for ln in block.split("\n"):
        m = _RECALL_LINE.match(ln)
        if m:
            text = m.group("text").strip()
            source = m.group("source").strip()
            ctx = [*messages, {"role": "system", "content": running}]
            if _known(ctx, "memory", [source], text):
                ln = pointer("memory", source, text)
                changed = True
        out.append(ln)
        running += ln + "\n"
    return "\n".join(out) if changed else block


def _shrink_okf(messages, block: str) -> str:
    if not _OKF_HEAD.match(block.lstrip()):
        return block
    parts = re.split(r"\n\n(?=(?:PROJECT MEMORY \(|LINKED MEMORY \(|GLOBAL MEMORY:))",
                     block.strip())
    running: list = list(messages)
    rendered: list[str] = []
    changed = False
    acc = ""
    for part in parts:
        m = _OKF_HEAD.match(part)
        if not m:
            rendered.append(part)
            acc = (acc + "\n\n" + part).strip()
            continue
        header = m.group(1)
        body = part[m.end():].strip()
        idents = [header]
        inner = re.search(r"\(([^)]+)\)", header)
        if inner:
            idents.append(inner.group(1))
        if len(body) >= 80:
            # The Doer seed labels this same brief "MEMORY (prior facts …)"
            # and does not repeat the PROJECT/LINKED/GLOBAL header. The body
            # still has to match; a different page under that label does not.
            idents.append("MEMORY (prior facts")
        if body and _known(running, "okf", idents, body):
            part = header + ":\n" + pointer("okf", header, body)
            changed = True
        rendered.append(part)
        acc = (acc + "\n\n" + part).strip()
        running = [*messages, {"role": "system", "content": acc}]
    return "\n\n".join(rendered) if changed else block


# ── tool results ────────────────────────────────────────────────────────


def _frontmatter(text: str) -> tuple[dict, str]:
    m = _FM.match(text or "")
    if not m:
        return {}, text or ""
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, val = line.split(":", 1)
        meta[key.strip().lower()] = val.strip().strip("\"'")
    return meta, (m.group(2) or "").strip()


def _artifact_kind(path: str) -> str | None:
    p = (path or "").replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    if base == "SKILL.md" or "/skills/" in p:
        return "skill"
    if base == "WORKFLOW.md" or "/workflows/" in p:
        return "workflow"
    if "/okf/" in p or (base.startswith("compacted-") and base.endswith(".md")):
        return "okf"
    return None


def _file_idents(path: str, meta: dict) -> list[str]:
    p = (path or "").replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    parent = p.rsplit("/", 2)[-2] if "/" in p else ""
    stem = os.path.splitext(base)[0]
    idents = [meta.get("name") or "", meta.get("id") or "", meta.get("title") or "",
              parent, stem, p, path or ""]
    return [i for i in idents if i and len(i) >= 2]


def _pointer_for_file(messages, path: str, content: str) -> str | None:
    kind = _artifact_kind(path)
    if not kind:
        return None
    meta, body = _frontmatter(content)
    body_s = body.strip()
    idents = _file_idents(path, meta)
    # A page whose body is already present (the injected playbook, a prior
    # read, or the write that just stored it). A longer on-disk body than the
    # copy above does not match, so the fuller text is still sent.
    if body_s and _known(messages, kind, idents, body_s):
        ident = next((i for i in idents if i), path)
        return pointer(kind, ident, body_s)
    if content.strip() and content.strip() != body_s and _known(
            messages, kind, idents, content.strip()):
        ident = next((i for i in idents if i), path)
        return pointer(kind, ident, content.strip())
    return None


def _dedupe_read(messages, name: str, args: dict, result: dict):
    if name == "read_files":
        content = result.get("content")
        if not isinstance(content, str) or not content:
            return result
        new = _dedupe_read_files(messages, content)
        if new == content:
            return result
        return {**result, "content": new}
    if name == "file_read":
        content = result.get("content")
        path = str(args.get("path") or result.get("path") or "")
    elif name == "read_lines":
        content = result.get("text")
        path = str(result.get("path") or args.get("path") or "")
    else:
        return result
    if not isinstance(content, str) or not path:
        return result
    repl = _pointer_for_file(messages, path, content)
    if repl is None:
        return result
    key = "text" if name == "read_lines" else "content"
    return {**result, key: repl, "already_in_context": True, "path": path}


def _dedupe_read_files(messages, content: str) -> str:
    lines = content.split("\n")
    out: list[str] = []
    i = 0
    changed = False
    while i < len(lines):
        m = _READ_HEAD.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        path = m.group(1).strip()
        j = i + 1
        while j < len(lines) and not _READ_HEAD.match(lines[j]):
            j += 1
        body = "\n".join(lines[i + 1:j])
        repl = _pointer_for_file(messages, path, body)
        out.append(lines[i])
        if repl is None:
            out.extend(lines[i + 1:j])
        else:
            out.append(repl)
            changed = True
        i = j
    return "\n".join(out) if changed else content


def _dedupe_memory(messages, name: str, result: dict):
    if name == "memory_write":
        text = result.get("text")
        if not isinstance(text, str) or not _known(
                messages, "memory", ["memory_write", str(result.get("id") or "")],
                text):
            return result
        ident = str(result.get("id") or "memory_write")
        return {**result, "text": pointer("memory", ident, text),
                "already_in_context": True}
    hits = result.get("hits")
    if not isinstance(hits, list):
        return result
    new_hits = []
    changed = False
    running = list(messages)
    for hit in hits:
        if not isinstance(hit, dict):
            new_hits.append(hit)
            continue
        body = str(hit.get("text") or hit.get("body") or "")
        idents = [str(hit.get(k) or "") for k in
                  ("source_uri", "id", "path", "source")]
        idents = [i for i in idents if i]
        if body and _known(running, "memory", idents, body):
            ident = idents[0] if idents else "memory"
            replaced = pointer("memory", ident, body)
            updated = dict(hit)
            if "text" in hit or "body" not in hit:
                updated["text"] = replaced
            if "body" in hit:
                updated["body"] = replaced
            updated["already_in_context"] = True
            new_hits.append(updated)
            changed = True
        else:
            new_hits.append(hit)
        running = [*running, {"role": "user", "content": str(new_hits[-1])}]
    if not changed:
        return result
    return {**result, "hits": new_hits}


def _dedupe_playbook_hits(messages, name: str, result: dict, cwd):
    kind = "workflow" if name == "workflow_search" else "skill"
    key = "workflows" if kind == "workflow" else "skills"
    hits = result.get(key)
    if not isinstance(hits, list) or not hits:
        return result
    pool = _playbook_pool(kind, cwd)
    new_hits = []
    changed = False
    running = list(messages)
    for hit in hits:
        if not isinstance(hit, dict):
            new_hits.append(hit)
            continue
        updated, did = _one_playbook_hit(running, kind, hit, pool)
        new_hits.append(updated)
        changed = changed or did
        running = [*running, {"role": "user", "content": str(updated)}]
    if not changed:
        return result
    return {**result, key: new_hits}


def _playbook_pool(kind: str, cwd) -> dict:
    try:
        if kind == "workflow":
            from aiforge_core.runtime import workflows as reg
        else:
            from aiforge_core.runtime import skills as reg
        return {s.name: s for s in reg.load(cwd)}
    except Exception:  # noqa: BLE001
        return {}


def _one_playbook_hit(messages, kind: str, hit: dict, pool: dict) -> tuple[dict, bool]:
    sk = pool.get(hit.get("name"))
    body = hit.get("body")
    if not isinstance(body, str) or not body.strip():
        body = getattr(sk, "body", None) if sk is not None else None
    if not isinstance(body, str) or not body.strip():
        return hit, False
    idents = [str(hit.get("name") or "")]
    source = str(hit.get("source") or getattr(sk, "source", "") or "")
    if source:
        idents.append(source)
    if not _known(messages, kind, idents, body):
        if hit.get("body") == body:
            return hit, False
        # First time this body is needed: attach it intact. Search results
        # used to carry only the name, so a playbook that was not injected
        # (or was cut short) never actually arrived.
        return {**hit, "body": body}, True
    ident = idents[0] or kind
    updated = {**hit, "body": pointer(kind, ident, body), "already_in_context": True}
    return updated, True
