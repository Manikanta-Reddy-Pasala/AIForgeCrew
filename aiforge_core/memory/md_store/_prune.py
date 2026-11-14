"""md_store internals: move run narration, toy-task topics and off-topic facts
out of the briefs and OKF learning nodes. No model.

Nothing is deleted: every moved fact is written to
``archive/<stamp>/moved-facts.jsonl`` (file, section, reason, text) and every
retired brief / node is moved whole under ``archive/<stamp>/``, so a wrong call
is one copy back. Each pass has its own off switch:

* ``AIFORGE_COMPACT_JUNK_SWEEP=0``  — keep toy / all-narration topics and
  off-topic facts (:func:`sweep_junk_topics`)
* ``AIFORGE_COMPACT_NARRATION=0``   — keep narration bullets (:func:`prune_narration`)
"""
from __future__ import annotations

import json
import os
import re
import shutil

from ._base import _WRITE_LOCK, _log, _now_iso, iter_briefs, memory_dir
from ._prune_classify import is_narration, is_toy, off_topic

_LISTS = ("key_results", "facts", "learnings")
# Briefs every recall reads; never retired whole by the junk sweep.
_PROTECTED_RE = re.compile(r"^compacted-(?:global|shared|prefs|rules?)(?:-\d+)*\.md$")


def _on(var: str) -> bool:
    return os.environ.get(var, "1") != "0"


class _Archive:
    """One archive folder per pass, created on first use."""

    def __init__(self) -> None:
        self.dir = memory_dir() / "archive" / _now_iso().replace(":", "")
        self.records = 0

    def record(self, path, section: str, reason: str, text: str) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        rel = os.path.relpath(str(path), str(memory_dir()))
        with open(self.dir / "moved-facts.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"file": rel, "section": section,
                                 "reason": reason, "text": text},
                                ensure_ascii=False) + "\n")
        self.records += 1
        _log.info("prune: moved out %s [%s/%s]: %.80s", rel, section, reason, text)

    def move_file(self, path, reason: str) -> bool:
        rel = os.path.relpath(str(path), str(memory_dir()))
        dst = self.dir / rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), str(dst))
        except OSError as exc:
            _log.warning("prune: could not archive %s: %s", rel, exc)
            return False
        self.record(path, "*", reason, f"file archived to {dst}")
        return True


def _brief_items(path):
    from aiforge_core.runtime import work_notes
    parsed = work_notes.parse_note(path.read_text(encoding="utf-8", errors="replace"))
    sec = parsed["sections"]
    return parsed, {k: [work_notes.clean_item(x) for x in sec.get(k) or []]
                    for k in _LISTS}


def _node_items(d) -> list[str]:
    from aiforge_core.runtime import work_notes
    body = (d.get("body") or "").strip()
    if not body.startswith("- "):
        return []
    return [work_notes.clean_item(ln) for ln in body.splitlines() if ln.strip()]


def _learning_nodes():
    from aiforge_core.memory.okf import store
    return [d for d in store.load_all() if d.get("type") == "learning"]


def _save_node(d, items: list[str]) -> bool:
    from aiforge_core.memory.okf import store
    body = "\n".join(f"- {x}" for x in items)
    return bool(store.save_node("learning", d["id"], d.get("meta") or {}, body,
                                reindex=False).get("ok"))


def _reindex_okf() -> None:
    from aiforge_core.memory.okf import store
    store._invalidate()
    store._write_index()


def _is_junk(items: list[str]) -> bool:
    return bool(items) and all(is_narration(x) or is_toy(x) for x in items)


def _topic_of_brief(path, parsed) -> str:
    key = str((parsed.get("frontmatter") or {}).get("key") or "")
    return key or re.sub(r"(?:-\d+)+$", "", path.stem[len("compacted-"):])


def _sweep_brief(path, arch: _Archive, out: dict) -> None:
    from aiforge_core.runtime import work_notes
    parsed, sec = _brief_items(path)
    items = [x for k in _LISTS for x in sec[k]]
    if (not _PROTECTED_RE.match(path.name) and not parsed["body"].strip()
            and _is_junk(items)):
        for k in _LISTS:
            for x in sec[k]:
                arch.record(path, k, "junk-topic", x)
        if arch.move_file(path, "junk-topic"):
            out["briefs_archived"] += 1
        return
    if _PROTECTED_RE.match(path.name):
        return
    flat = [(k, x) for k in _LISTS for x in sec[k]]
    off = set(off_topic(_topic_of_brief(path, parsed), [x for _, x in flat]))
    if not off:
        return
    keep = {k: [] for k in _LISTS}
    for i, (k, x) in enumerate(flat):
        if i in off:
            arch.record(path, k, "off-topic", x)
        else:
            keep[k].append(x)
    if work_notes.update_note(str(path), **keep).get("ok"):
        out["off_topic_facts"] += len(off)


def _sweep_node(d, arch: _Archive, out: dict) -> bool:
    items = _node_items(d)
    if not items:
        return False
    if _is_junk(items):
        for x in items:
            arch.record(d["path"], "body", "junk-topic", x)
        if arch.move_file(d["path"], "junk-topic"):
            out["nodes_archived"] += 1
            return True
        return False
    topic = str((d.get("meta") or {}).get("category") or "")
    off = set(off_topic(topic, items))
    if not off:
        return False
    for i in sorted(off):
        arch.record(d["path"], "body", "off-topic", items[i])
    if _save_node(d, [x for i, x in enumerate(items) if i not in off]):
        out["off_topic_facts"] += len(off)
        return True
    return False


def sweep_junk_topics() -> dict:
    """Archive every brief / OKF learning node whose facts are ALL narration or
    toy-task facts, and move out facts unrelated to their brief's topic
    (see ``off_topic``). Never raises; one bad file never stops the pass."""
    if not _on("AIFORGE_COMPACT_JUNK_SWEEP"):
        return {"ok": True, "skipped": "disabled"}
    arch = _Archive()
    out = {"ok": True, "briefs_archived": 0, "nodes_archived": 0,
           "off_topic_facts": 0, "errors": 0}
    with _WRITE_LOCK:
        for p in iter_briefs():
            try:
                _sweep_brief(p, arch, out)
            except Exception as exc:  # noqa: BLE001
                out["errors"] += 1
                _log.warning("junk sweep: %s skipped: %s", p.name, exc)
        changed = False
        try:
            for d in _learning_nodes():
                try:
                    changed = _sweep_node(d, arch, out) or changed
                except Exception as exc:  # noqa: BLE001
                    out["errors"] += 1
                    _log.warning("junk sweep: node %s skipped: %s", d.get("id"), exc)
        except Exception as exc:  # noqa: BLE001
            out["errors"] += 1
            _log.warning("junk sweep: okf nodes skipped: %s", exc)
        if changed:
            _reindex_okf()
    out["archive"] = str(arch.dir) if arch.records else ""
    out["moved"] = arch.records
    return out


def _prune_brief(path, arch: _Archive) -> int:
    from aiforge_core.runtime import work_notes
    _parsed, sec = _brief_items(path)
    keep = {k: [x for x in sec[k] if not is_narration(x)] for k in _LISTS}
    dropped = [(k, x) for k in _LISTS for x in sec[k] if is_narration(x)]
    if not dropped:
        return 0
    if not any(keep.values()):
        return 0                   # all narration: the junk sweep's call
    for k, x in dropped:
        arch.record(path, k, "narration", x)
    return len(dropped) if work_notes.update_note(str(path), **keep).get("ok") else 0


def prune_narration() -> dict:
    """Move run-narration bullets out of every brief and OKF learning node
    (into the archive record). A file left with no knowledge at all is not
    emptied here — :func:`sweep_junk_topics` archives it whole. Never raises."""
    if not _on("AIFORGE_COMPACT_NARRATION"):
        return {"ok": True, "skipped": "disabled"}
    arch = _Archive()
    out = {"ok": True, "brief_bullets": 0, "node_bullets": 0, "errors": 0}
    with _WRITE_LOCK:
        for p in iter_briefs():
            try:
                out["brief_bullets"] += _prune_brief(p, arch)
            except Exception as exc:  # noqa: BLE001
                out["errors"] += 1
                _log.warning("narration prune: %s skipped: %s", p.name, exc)
        try:
            for d in _learning_nodes():
                items = _node_items(d)
                keep = [x for x in items if not is_narration(x)]
                if not keep or len(keep) == len(items):
                    continue
                for x in items:
                    if is_narration(x):
                        arch.record(d["path"], "body", "narration", x)
                if _save_node(d, keep):
                    out["node_bullets"] += len(items) - len(keep)
            if out["node_bullets"]:
                _reindex_okf()
        except Exception as exc:  # noqa: BLE001
            out["errors"] += 1
            _log.warning("narration prune: okf nodes skipped: %s", exc)
    out["archive"] = str(arch.dir) if arch.records else ""
    return out
