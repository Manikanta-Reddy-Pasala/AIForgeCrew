"""User-given targets: data the user pasted with "match this output" and the
like. Stored verbatim on disk per chat, pinned in the prompt every turn, and
tied to an enforced board item that closes only when a compare command on
the file ran and passed. Off switch: ``AIFORGE_CHAT_TARGETS=0``.

Store: ``<chat scratch>/session-<id>/.aiforge/targets/`` — one file per
target version (``target-1.json``, ``target-1.v2.json``, …) plus the index
``targets.json`` (a list of rows: id, label, path, bytes, sha256, turn,
verified, versions, truncated, created)."""
from __future__ import annotations

import builtins
import hashlib
import json
import logging
import os
import re
import time

from aiforge_core.config import _atomic

log = logging.getLogger("aiforge.targets")

#: A stored payload is cut here; the row then carries ``truncated: true``.
MAX_BYTES = 2 * 1024 * 1024
#: A fence counts as the cue's payload when it starts within this many chars.
_GAP = 300
_INDEX = "targets.json"
_LABELS = ("output", "input", "result", "response", "data", "file")

_NOUN = r"output|input|result|response|data|file|json|csv|xml|yaml"
_DET = r"(?:this|the|that|below|following|my|our)"
# Every alternative starts at a word boundary; the bare "target:" form needs
# its colon so a line like "target the users table" is not a cue.
_CUE = re.compile(
    r"\b(?:"
    r"(?:must |should )?match(?:es|ing)? " + _DET
    + r" (?:new |exact |expected )?(?P<n1>" + _NOUN + r")\b"
    r"|(?:must|should) (?:equal|match)\b"
    r"|(?:expected|target) (?P<n2>" + _NOUN + r")\b"
    r"|should (?:look|be) (?:like|exactly)\b"
    r"|compare (?:it |this |that |them )?(?:with|against|to) this\b"
    r"|set target:?(?:[ \t]+(?P<n3>[a-z][a-z0-9_-]*)(?=[ \t]*(?:[=:]|$)))?"
    r"|^target:(?:[ \t]*(?P<n4>[a-z][a-z0-9_-]*)(?=[ \t]*(?:[=:]|$)))?"
    r")", re.I | re.M)
_FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
# The rest of the cue's line for an unfenced paste: a short tail, no further
# sentence punctuation, optionally ending in ":" or "=". Rules out prose like
# "the expected output is wrong, fix it" followed by more prose.
_TAIL = re.compile(r"[^\n.,;!?]{0,40}[:=]?[ \t]*")
_REASK = re.compile(
    r"\b(?:paste\b[^.?!\n]{0,60}?\bagain"
    r"|(?:please |(?:can|could|would) you |you to |you )paste "
    r"(?:the |your |me the )?(?:expected|target|sample|original|reference)"
    r"|share (?:the|your|me the) expected"
    r"|what (?:was|is) the (?:expected|data|output|input)\b"
    r"[^.?!\n]{0,80}?\b(?:gave|shared|pasted|sent|earlier)"
    r"|send (?:me )?(?:a |the )?(?:sample|expected)"
    r"|provide (?:the |a )?(?:expected|sample)"
    r"|re-?(?:send|paste|share) (?:the|your|it|that)"
    r")\b", re.I)
_COMPARE_TOOL = re.compile(
    r"\b(?:diff|cmp|jq|csvdiff|deepdiff|difflib|filecmp|compare|assert)\b")
_WARNED: set[str] = set()


def enabled() -> bool:
    val = os.environ.get("AIFORGE_CHAT_TARGETS", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def min_chars() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_CHAT_TARGET_MIN_CHARS", "200")))
    except ValueError:
        return 200


def dir(session_id) -> str:  # noqa: A001 — mirrors chat_media.media_dir
    from aiforge_core.runtime import repo_ident
    return os.path.join(repo_ident.chat_scratch_root(), f"session-{session_id}",
                        ".aiforge", "targets")


def _index(session_id) -> str:
    return os.path.join(dir(session_id), _INDEX)


# --- index -----------------------------------------------------------------

def _read(path: str, *, for_write: bool = False) -> builtins.list[dict]:
    """The rows in ``path``; [] when missing. On a read: any other failure is
    logged once and gives []. Before a write: a read error other than
    "missing" is re-raised so the caller never overwrites an index it could
    not read, and a corrupt file is moved aside to ``<path>.bad-<epoch>``."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except OSError as exc:
        if for_write:
            log.warning("targets: cannot read %s before writing it: %s", path, exc)
            raise
        return _warn_once(path, f"cannot read ({exc})")
    except ValueError as exc:
        return _corrupt(path, f"not valid JSON ({exc})", for_write)
    if not isinstance(data, builtins.list):
        return _corrupt(path, "top level is not a list", for_write)
    return [r for r in data if isinstance(r, dict) and r.get("id") and r.get("path")]


def _warn_once(path: str, why: str) -> builtins.list[dict]:
    if path not in _WARNED:
        _WARNED.add(path)
        log.warning("targets: ignoring %s: %s", path, why)
    return []


def _corrupt(path: str, why: str, for_write: bool) -> builtins.list[dict]:
    if not for_write:
        return _warn_once(path, why)
    bad = f"{path}.bad-{int(time.time())}"
    try:
        os.replace(path, bad)
        log.warning("targets: %s: %s; moved aside to %s", path, why, bad)
    except OSError as exc:
        log.warning("targets: %s: %s; could not move aside (%s)", path, why, exc)
        raise
    return []


def _save_index(session_id, rows) -> None:
    _atomic.write_text(_index(session_id), json.dumps(rows, indent=2) + "\n")


def list(session_id) -> builtins.list[dict]:  # noqa: A001
    if not enabled() or session_id is None:
        return []
    return _read(_index(session_id))


# --- capture ---------------------------------------------------------------

def _sniff(text: str) -> str:
    t = text.strip()
    if t[:1] in "{[":
        try:
            json.loads(t)
            return "json"
        except ValueError:
            pass
    if t.startswith("<"):
        return "xml"
    lines = [ln for ln in t.splitlines() if ln.strip()]
    if len(lines) >= 2:
        n = lines[0].count(",")
        if n >= 1 and all(ln.count(",") == n for ln in lines[:20]):
            return "csv"
    if re.match(r"^[\w-]+:\s", t):
        return "yaml"
    return "txt"


def _label(m: re.Match) -> str:
    for g in ("n1", "n2", "n3", "n4"):
        word = m.group(g)
        if word:
            word = word.lower()
            if g in ("n3", "n4") or word in _LABELS:
                return word
    return "output"


def _tail_ok(text: str, m: re.Match) -> bool:
    end = text.find("\n", m.end())
    tail = text[m.end():] if end < 0 else text[m.end():end]
    return _TAIL.fullmatch(tail) is not None


def _payloads(text: str) -> builtins.list[tuple[str, str]]:
    """(label, body) per cue that has a payload. A cue claims the first fence
    after it (within _GAP chars, before the next cue), else the unfenced rest
    of its paragraph when the cue line is a short lead-in and the rest is at
    least min_chars long. Cues inside fences are not cues."""
    fences = builtins.list(_FENCE.finditer(text))
    inside = [(f.start(), f.end()) for f in fences]
    cues = [m for m in _CUE.finditer(text)
            if not any(a <= m.start() < b for a, b in inside)]
    out: builtins.list[tuple[str, str]] = []
    used: set[int] = set()
    for i, m in enumerate(cues):
        region_end = cues[i + 1].start() if i + 1 < len(cues) else len(text)
        label = _label(m)
        fence = next((f for f in fences
                      if f.start() not in used and m.end() <= f.start() < region_end
                      and f.start() - m.end() <= _GAP), None)
        if fence is not None:
            used.add(fence.start())
            out.append((label, fence.group(1)))
            continue
        if not _tail_ok(text, m):
            continue
        rest = text[m.end():region_end].lstrip(" \t:=\n")
        if any(a < region_end and b > m.end() for a, b in inside):
            continue  # a fence sits in the rest but too far away: not a paste
        if len(rest) >= min_chars():
            out.append((label, rest))
    if not out and cues and fences:
        # the cue follows the payload: "```…```\nthat is the expected output"
        m = cues[0]
        fence = next((f for f in reversed(fences)
                      if f.end() <= m.start() and m.start() - f.end() <= _GAP), None)
        if fence is not None:
            out.append((_label(m), fence.group(1)))
    return out


def _store(session_id, rows, label: str, body: str, turn: int | None) -> dict:
    body = body.strip("\n") + "\n"
    raw = body.encode("utf-8")
    truncated = len(raw) > MAX_BYTES
    if truncated:
        body = raw[:MAX_BYTES].decode("utf-8", errors="ignore")
        if not body.endswith("\n"):
            body += "\n"
        raw = body.encode("utf-8")
    sha = hashlib.sha256(raw).hexdigest()
    hit = next((r for r in rows if r.get("label") == label), None)
    if hit is not None and hit.get("sha256") == sha:
        return hit
    ext = _sniff(body)
    if hit is not None:
        n = len(hit.get("versions") or []) + 1
        fname = f"{hit['id']}.v{n}.{ext}"
        row = hit
    else:
        tid = f"target-{len(rows) + 1}"
        fname = f"{tid}.{ext}"
        row = {"id": tid, "label": label, "versions": [],
               "turn": turn if turn is not None else len(rows) + 1}
        rows.append(row)
    path = os.path.join(dir(session_id), fname)
    _atomic.write_text(path, body)
    row.update({"path": path, "bytes": len(raw), "sha256": sha, "verified": None,
                "truncated": truncated, "created": int(time.time())})
    row["versions"] = (row.get("versions") or []) + [path]
    if turn is not None:
        row["turn"] = turn
    return row


def capture(session_id, text: str, turn: int | None = None) -> builtins.list[dict]:
    """Save the target(s) a user message carries. Returns the new/updated rows.
    Raises OSError when an existing index cannot be read: it is never
    overwritten blind (a corrupt one is moved aside instead)."""
    if not enabled() or not text or session_id is None:
        return []
    found = _payloads(text)
    if not found:
        return []
    rows = _read(_index(session_id), for_write=True)
    out = []
    for label, body in found:
        row = _store(session_id, rows, label, body, turn)
        if row not in out:
            out.append(row)
    _save_index(session_id, rows)
    return out


def mark_verified(session_id, target_id: str, evidence: str) -> None:
    if not enabled() or session_id is None:
        return
    rows = _read(_index(session_id), for_write=True)
    hit = False
    for r in rows:
        if r["id"] == target_id:
            r["verified"] = {"at": int(time.time()), "evidence": str(evidence)[:1000]}
            hit = True
    if hit:
        _save_index(session_id, rows)


# --- prompt, board, facts --------------------------------------------------

def _preview(path: str, n: int = 300) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(n + 1)[:n].replace("\n", " ⏎ ")
    except OSError:
        return "(unreadable)"


def block(session_id) -> str:
    rows = list(session_id)
    if not rows:
        return ""
    lines = ["TARGETS — data the user gave to match. Each is on disk, verbatim. Read it "
             "with file_read. Never ask the user to paste or repeat it. A target counts "
             "as matched only after you run a compare command (diff/cmp/jq/python) on the "
             "file and it passes; the harness closes the board item from that command."]
    for r in rows:
        ok = "yes" if r.get("verified") else "no"
        cut = f" (cut at {MAX_BYTES // (1024 * 1024)} MB)" if r.get("truncated") else ""
        name = os.path.basename(r["path"])
        lines.append(f"- {r['id']} ({r['label']}, {name}, {r.get('bytes', 0)} B{cut}) "
                     f"— verified: {ok} — full: file_read {r['path']}\n"
                     f"  preview: {_preview(r['path'])}")
    return "\n".join(lines)


def board_items(session_id) -> dict:
    """Enforced board items for unverified targets (slug → item)."""
    out = {}
    for r in list(session_id):
        if r.get("verified"):
            continue
        rel = os.path.join("targets", os.path.basename(r["path"]))
        out[r["id"]] = {"title": f"{r['label']} matches {r['id']} — check: "
                                 f"a compare command on {rel} ran and passed",
                        "status": "pending", "enforced": True, "proof": r["id"]}
    return out


def _mentions(cmd: str, path: str) -> bool:
    """True when ``cmd`` names the target's current file: ``…/targets/<name>``
    in any absolute or relative spelling, or the bare ``<name>`` (a copy)."""
    name = re.escape(os.path.basename(path))
    return (re.search(r"(?<![\w.-])targets/" + name + r"(?![\w.-])", cmd) is not None
            or re.search(r"(?<![\w./-])" + name + r"(?![\w.-])", cmd) is not None)


def is_compare(cmd: str, session_id) -> str | None:
    """The target id a shell command compares against, else None. Needs a
    compare tool (diff, cmp, jq, compare, assert, …) in the command text
    outside the file path, and the target's current file named in it."""
    c = " ".join(str(cmd or "").split())
    if not c:
        return None
    for r in list(session_id):
        if not _mentions(c, r["path"]):
            continue
        rest = re.sub(r"\S*" + re.escape(os.path.basename(r["path"])) + r"\S*", " ", c)
        if _COMPARE_TOOL.search(rest):
            return r["id"]
    return None


def asks_for_target(text: str) -> bool:
    return bool(_REASK.search(text or ""))


def facts(session_id) -> builtins.list[str]:
    """Facts-line fragments, e.g. 'target-1: matched' / 'target-1: not yet compared'."""
    return [f"{r['id']}: " + ("matched" if r.get("verified") else "not yet compared")
            for r in list(session_id)]
