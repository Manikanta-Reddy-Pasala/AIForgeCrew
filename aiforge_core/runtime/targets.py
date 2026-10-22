"""User-given targets: data the user pasted with "match this output" and the
like. Stored verbatim on disk per chat, pinned in the prompt every turn, and
tied to an enforced board item that closes only when a compare command on
the file ran and passed. Off switch: ``AIFORGE_CHAT_TARGETS=0``.

Store: ``<chat scratch>/session-<id>/.aiforge/targets/`` — one file per
target version (``target-1.json``, ``target-1.v2.json``, …) plus the index
``targets.json`` (a list of rows: id, label, path, bytes, sha256, turn,
verified, last_compare, versions, truncated, created). Paths in rows, the
TARGETS block and board items are absolute — one spelling everywhere."""
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
_LABELS = ("output", "input", "result", "response", "data")

# The spec's cue list, no wider: every alternative starts at a word boundary,
# "must match this <noun>" carries its noun, and the bare "target:" form
# needs its colon so a line like "target the users table" is not a cue.
_CUE = re.compile(
    r"\b(?:"
    r"(?:must )?match (?:this|the|below|following) (?P<n1>output|input|result|response|data)\b"
    r"|must (?:equal|match)\b"
    r"|expected (?P<n2>output|result|response)\b"
    r"|should (?:look|be) (?:like|exactly)\b"
    r"|compare (?:with|against|to) this\b"
    r"|exactly (?:this|the following|these) (?P<n5>output|result|response|data|lines?|json)\b"
    r"|(?:prints?|outputs?|returns?) exactly\b"
    r"|set target:?(?:[ \t]+(?P<n3>[a-z][a-z0-9_-]*)(?=[ \t]*(?:[=:]|$)))?"
    r"|^target:(?:[ \t]*(?P<n4>[a-z][a-z0-9_-]*)(?=[ \t]*(?:[=:]|$)))?"
    r")", re.I | re.M)
_FENCE = re.compile(r"```[^\n`]*\n(.*?)```", re.S)
# The rest of the cue's line: a short lead-in (≤40 chars), no sentence
# punctuation, optionally ending in ":" or "=". A cue line that reads as prose
# ("the expected output is wrong, fix it", "…exactly like before. Run:") is
# not a capture, fenced or not.
_TAIL = re.compile(r"[^\n.,;!?]{0,40}[:=]?[ \t]*")
# A cue line with a fence right after it may run longer ("prints exactly this
# output (keys in this order, values rounded):") when it ends in ":" and says
# nothing that makes the match partial or loose.
_FENCED_TAIL = re.compile(r"[^\n]{0,200}:[ \t]*")
_HEDGE = re.compile(r"\b(?:but|only|except|apart|similar|roughly|approximately|"
                    r"partly|partial|some of|like|before|after)\b", re.I)
# Unfenced text that is not json/csv/xml/yaml and reads as sentences is prose.
_SENTENCE_END = re.compile(r"[.!?](?=\s|$)")
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
# Real comparison tools only. ``jq`` is not one: ``jq . <target>`` prints;
# ``jq … && diff a b`` counts through its ``diff``.
_COMPARE_TOOL = re.compile(
    r"\b(?:diff|cmp|compare|assert|csvdiff|deepdiff|difflib|filecmp)\b")
_WARNED: set[str] = set()


def enabled() -> bool:
    val = os.environ.get("AIFORGE_CHAT_TARGETS", "1").strip().lower()
    return val not in ("0", "false", "no", "off")


def min_chars() -> int:
    try:
        return max(1, int(os.environ.get("AIFORGE_CHAT_TARGET_MIN_CHARS", "200")))
    except ValueError:
        return 200


# ``dir`` and ``list`` below are the plan's public names and SHADOW the
# builtins inside this module: use ``builtins.list`` for a real list here.
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
    return [r for r in data
            if isinstance(r, dict) and r.get("id") and r.get("label") and r.get("path")]


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
    for g in ("n1", "n2", "n3", "n4", "n5"):
        word = m.group(g)
        if word:
            word = word.lower()
            if g in ("n3", "n4") or word in _LABELS:
                return word
    return "output"


def _line_end(text: str, pos: int) -> int:
    end = text.find("\n", pos)
    return len(text) if end < 0 else end


def _tail(text: str, m: re.Match) -> str:
    """The rest of the cue's line after the cue."""
    return text[m.end():_line_end(text, m.end())]


def _tail_ok(text: str, m: re.Match, *, allow_period: bool = False,
             fenced: bool = False) -> bool:
    tail = _tail(text, m)
    if allow_period and tail.rstrip().endswith("."):
        tail = tail.rstrip()[:-1]
    if _TAIL.fullmatch(tail) is not None:
        return True
    return (fenced and _FENCED_TAIL.fullmatch(tail) is not None
            and not _HEDGE.search(tail))


def _is_prose(rest: str) -> bool:
    """Unfenced text of no recognised shape with two or more sentence ends in
    its first 200 chars is prose, not data."""
    return _sniff(rest) == "txt" and len(_SENTENCE_END.findall(rest[:200])) >= 2


def _payloads(text: str) -> builtins.list[tuple[str, str]]:
    """(label, body) per cue that has a payload. A cue claims the first fence
    after it (within _GAP chars, before the next cue), else the unfenced rest
    of its paragraph when the rest is at least min_chars long and not prose.
    Either way the cue line must be a short lead-in (_TAIL). Cues inside
    fences are not cues. The unfenced body is verbatim: it starts on the line
    after the cue line unless the cue line ends right at the cue (or in ":"
    / "="), in which case it starts right after that."""
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
        if not _tail_ok(text, m, fenced=fence is not None):
            continue
        if fence is not None:
            used.add(fence.start())
            out.append((label, fence.group(1)))
            continue
        if any(a < region_end and b > m.end() for a, b in inside):
            continue  # a fence sits in the rest but too far away: not a paste
        if _tail(text, m).strip(" \t:="):
            start = min(_line_end(text, m.end()) + 1, region_end)
            rest = text[start:region_end]
        else:
            rest = text[m.end():region_end].lstrip(" \t:=\n")
        if len(rest) >= min_chars() and not _is_prose(rest):
            out.append((label, rest))
    if not out and cues and fences:
        # the cue follows the payload: "```…```\nthat is the expected output."
        m = cues[0]
        fence = next((f for f in reversed(fences)
                      if f.end() <= m.start() and m.start() - f.end() <= _GAP), None)
        if fence is not None and _tail_ok(text, m, allow_period=True):
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
                "last_compare": None, "truncated": truncated, "created": int(time.time())})
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


def note_compare(session_id, target_id: str, ok: bool, evidence: str) -> None:
    """Record a compare command's outcome on a target. A pass verifies it; a
    fail is kept as ``last_compare`` (``verified`` untouched) so the facts
    line can say "differs"."""
    if not enabled() or session_id is None:
        return
    rows = _read(_index(session_id), for_write=True)
    stamp = {"ok": bool(ok), "at": int(time.time()), "evidence": str(evidence)[:1000]}
    hit = False
    for r in rows:
        if r.get("id") == target_id:
            r["last_compare"] = stamp
            if ok:
                r["verified"] = {"at": stamp["at"], "evidence": stamp["evidence"]}
            hit = True
    if hit:
        _save_index(session_id, rows)


def mark_verified(session_id, target_id: str, evidence: str) -> None:
    note_compare(session_id, target_id, True, evidence)


def _state(r: dict) -> str:
    """'matched', 'differs' or 'not yet compared'."""
    if r.get("verified"):
        return "matched"
    last = r.get("last_compare")
    if isinstance(last, dict) and last.get("ok") is False:
        return "differs"
    return "not yet compared"


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
             "as matched only after you run a compare command (diff/cmp/python assert) on "
             "the file and it passes; the harness closes the board item from that command. "
             "Compare without writing files into the project: pipe the output in, "
             "e.g. `<command> | diff - <target file>`."]
    for r in rows:
        state = _state(r)
        ok = "yes" if state == "matched" else "no"
        if state == "differs":
            ok += " (last compare differed)"
        cut = f" (cut at {MAX_BYTES // (1024 * 1024)} MB)" if r.get("truncated") else ""
        path = str(r.get("path") or "")
        lines.append(f"- {r.get('id')} ({r.get('label', 'output')}, {os.path.basename(path)}, "
                     f"{r.get('bytes', 0)} B{cut}) — verified: {ok} — full: file_read {path}\n"
                     f"  preview: {_preview(path)}")
    return "\n".join(lines)


def board_items(session_id) -> dict:
    """Enforced board items for unverified targets (slug → item)."""
    out = {}
    for r in list(session_id):
        if r.get("verified"):
            continue
        tid = str(r.get("id"))
        out[tid] = {"title": f"{r.get('label', 'output')} matches {tid} — check: "
                             f"a compare command on {r.get('path') or ''} ran and passed",
                    "status": "pending", "enforced": True, "proof": tid}
    return out


def _mentions(cmd: str, path: str) -> bool:
    """True when ``cmd`` names the target's current file: ``…/targets/<name>``
    in any absolute or relative spelling, or ``<name>`` / ``./<name>`` (a copy
    in the working dir)."""
    name = re.escape(os.path.basename(path))
    return (re.search(r"(?<![\w.-])targets/" + name + r"(?![\w.-])", cmd) is not None
            or re.search(r"(?<![\w./-])(?:\./)?" + name + r"(?![\w.-])", cmd) is not None)


def is_compare(cmd: str, session_id) -> str | None:
    """The target id a shell command compares against, else None. Needs a
    real comparison tool (diff, cmp, compare, assert, csvdiff, deepdiff,
    difflib, filecmp) in the command text outside the file path, and the
    target's current file named in it."""
    c = " ".join(str(cmd or "").split())
    if not c:
        return None
    for r in list(session_id):
        path = str(r.get("path") or "")
        if not path or not _mentions(c, path):
            continue
        rest = re.sub(r"\S*" + re.escape(os.path.basename(path)) + r"\S*", " ", c)
        if _COMPARE_TOOL.search(rest):
            return r.get("id")
    return None


def asks_for_target(text: str) -> bool:
    return bool(_REASK.search(text or ""))


def facts(session_id) -> builtins.list[str]:
    """Facts-line fragments: 'target-1: matched' / 'differs' / 'not yet compared'."""
    return [f"{r.get('id')}: {_state(r)}" for r in list(session_id)]
