"""Chat projects: the folders under the mounted repos root, and each project's
memory.

A project is a direct child folder of the repos root. Its memory key is the
same repo name every chat read/write already uses (``repo_ident.repo_name``),
so nothing here invents a second identity.

A project's compacted brief stays where the memory store keeps it
(``<memory>/compacted/compacted-<slug>.md``) and is MIRRORED, both ways, to
``<repo>/.aiforge/memory/MEMORY.md`` so it is readable, editable and
committable next to the code. Raw captures and the search index never go into
the repo. Nothing outside ``<repo>/.aiforge/`` is ever written.

Management lives here too: the size cap and its compaction, the stale-fact
sweep, moving a fact to global, and forgetting a project.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
from pathlib import Path

from aiforge_core.config import _atomic

_log = logging.getLogger("aiforge.memory.projects")

# Where a chat with no project files what it learns. One shared bucket instead
# of a phantom ``session-<id>`` scope per chat that nothing ever reads again.
GENERAL = "general"

REPO_MEMORY_REL = os.path.join(".aiforge", "memory", "MEMORY.md")
_INSTRUCTION_NAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md", ".cursorrules")

_REG_LOCK = threading.RLock()
# The repos root as it was when the API started. ``AIFORGE_REPO_ROOT`` is
# rebound to ONE repo per ticket / team run, so it is only trustworthy as "the
# mounted folder" at import time.
_BOOT_REPO_ROOT = (os.environ.get("AIFORGE_REPO_ROOT") or "").strip()


# ── settings ─────────────────────────────────────────────────────────────────

def _int_env(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except (TypeError, ValueError):
        return default


def memory_cap() -> int:
    """Chars of knowledge a project brief may hold before it is compacted
    (``AIFORGE_PROJECT_MEMORY_CAP``)."""
    return max(2000, _int_env("AIFORGE_PROJECT_MEMORY_CAP", 24000))


def _compact_role() -> str:
    return (os.environ.get("AIFORGE_PROJECT_COMPACT_ROLE") or "learner").strip()


# ── the repos root and its folders ───────────────────────────────────────────

def root() -> str:
    """The folder whose children are projects: the configured repos base, else
    ``AIFORGE_PROJECTS_ROOT``, else the folder mounted at start
    (``AIFORGE_REPO_ROOT``), else ``AIFORGE_WORKTREE_ROOT`` / ``~/codeRepo``."""
    from aiforge_core.config import repo_map
    try:
        stored = (repo_map._load().get("default_root") or "").strip()
    except Exception:  # noqa: BLE001
        stored = ""
    for cand in (stored, os.environ.get("AIFORGE_PROJECTS_ROOT", ""),
                 _BOOT_REPO_ROOT):
        cand = os.path.expanduser((cand or "").strip())
        if cand and os.path.isdir(cand):
            return cand
    return repo_map.default_root()


def key_for(path: str) -> str:
    """The memory key for a project folder — what chat recall and writeback
    already file under."""
    from aiforge_core.runtime import repo_ident
    return repo_ident.repo_name(path, sentinel="repo")


def slug_for(key: str) -> str:
    from aiforge_core.memory import md_store
    return md_store._slug(key)


def list_folders() -> list[dict]:
    """Direct child folders of the root, as projects. Hidden folders skipped."""
    base = root()
    out: list[dict] = []
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
    except OSError:
        return out
    for e in entries:
        try:
            if not e.is_dir() or e.name.startswith("."):
                continue
        except OSError:
            continue
        out.append({
            "name": e.name, "path": e.path,
            "is_git": os.path.exists(os.path.join(e.path, ".git")),
            "has_aiforge": os.path.isdir(os.path.join(e.path, ".aiforge")),
        })
    return out


def resolve(name: str) -> "str | None":
    """Absolute path of project ``name``: a direct child of the root, nothing
    else (no separators, no traversal)."""
    name = (name or "").strip()
    if not name or name in (".", "..") or os.path.basename(name) != name:
        return None
    path = os.path.join(root(), name)
    return path if os.path.isdir(path) else None


def project_of(cwd: "str | None") -> "str | None":
    """The project a chat cwd belongs to (the first folder under the root), or
    None when the cwd is not inside the root."""
    if not cwd:
        return None
    try:
        base = os.path.realpath(root())
        target = os.path.realpath(str(cwd))
    except Exception:  # noqa: BLE001
        return None
    if target == base or not target.startswith(base + os.sep):
        return None
    return target[len(base) + 1:].split(os.sep, 1)[0] or None


# ── registry ─────────────────────────────────────────────────────────────────

def _state_dir() -> Path:
    from aiforge_core.memory import md_store
    p = md_store.memory_dir() / "projects"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _registry_path() -> Path:
    return _state_dir() / "registry.json"


def _load_registry() -> dict:
    try:
        d = json.loads(_registry_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001 — missing/corrupt → empty
        return {}


def _save_registry(reg: dict) -> None:
    _atomic.write_text(str(_registry_path()),
                       json.dumps(reg, indent=2, sort_keys=True))


def registered() -> dict:
    # No lock: the file is replaced atomically, and a chat turn reads this on
    # its way to the brief — it must never wait behind a sync in flight.
    return _load_registry()


def entry(slug: str) -> "dict | None":
    return registered().get(slug)


def register(path: str) -> "dict | None":
    """Record a project folder so its brief can be mirrored into it. Idempotent;
    a moved folder updates the stored path."""
    if not path or not os.path.isdir(path):
        return None
    key = key_for(path)
    slug = slug_for(key)
    if not slug or slug in ("shared", GENERAL):
        return None
    with _REG_LOCK:
        reg = _load_registry()
        cur = reg.get(slug) or {}
        if cur.get("path") != path or cur.get("key") != key:
            cur.update({"name": os.path.basename(os.path.normpath(path)),
                        "path": path, "key": key, "slug": slug})
            cur.setdefault("created", time.time())
            reg[slug] = cur
            _save_registry(reg)
        return dict(cur)


def _update_entry(slug: str, **fields) -> None:
    with _REG_LOCK:
        reg = _load_registry()
        if slug in reg:
            reg[slug].update(fields)
            _save_registry(reg)


# ── brief <-> repo file sync ─────────────────────────────────────────────────

def _sha(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def repo_memory_path(project_path: str) -> Path:
    return Path(project_path) / REPO_MEMORY_REL


def _base_path(slug: str) -> Path:
    """The last synced copy — the common ancestor a two-sided change is merged
    against."""
    return _state_dir() / f"{slug}.base.md"


def _read(path: Path) -> "str | None":
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _brief_body(slug: str) -> str:
    """The brief without its frontmatter — the text mirrored into the repo."""
    from aiforge_core.memory import md_store
    d = md_store.read_file(f"compacted-{slug}")
    return ((d or {}).get("body") or "").strip()


def _write_brief(slug: str, key: str, sections: dict) -> None:
    """Rewrite the brief from parsed sections and refresh its index row."""
    from aiforge_core.memory import md_store
    from aiforge_core.memory.md_store import _base, _ingest, _render
    path = md_store.brief_path(slug)
    with _base._WRITE_LOCK:
        _atomic.write_text(
            str(path),
            _render._render_brief(
                key, facts=sections.get("facts") or [],
                body_md=sections.get("body") or "",
                learnings=sections.get("learnings") or [],
                title=sections.get("title") or "",
                key_results=sections.get("key_results") or [],
                links=sections.get("links") or [],
                tags=sections.get("tags") or None,
                sources=sections.get("sources") or []))
    try:
        _ingest._ingest_one_md(path)
    except Exception as exc:  # noqa: BLE001 — the md is the source of truth
        _log.debug("projects: reingest of %s failed: %s", path.name, exc)


def _current_sections(slug: str) -> dict:
    from aiforge_core.memory import md_store
    from aiforge_core.memory.md_store import _render
    raw = _read(md_store.brief_path(slug)) or ""
    return _render._parse_brief(raw)


def _merge_repo_edit(slug: str, repo_txt: str, brief_changed: bool) -> dict:
    """Sections for the brief after an edit to the repo file.

    The repo file wins — it is what a person edited or pulled. Facts the store
    added since the last sync (a chat learned something meanwhile) are kept on
    top, found by comparing with the last synced copy, so neither side's change
    is lost and a fact the person deleted does not come back."""
    from aiforge_core.memory.md_store import _render
    new = _render._parse_brief(repo_txt)
    cur = _current_sections(slug)
    # Provenance lives only in the store's frontmatter; the repo file has none.
    new["sources"] = cur.get("sources") or []
    new["tags"] = new.get("tags") or cur.get("tags") or []
    if brief_changed:
        base = _render._parse_brief(_read(_base_path(slug)) or "")
        base_facts = set(base.get("facts") or [])
        have = set(new.get("facts") or [])
        for f in cur.get("facts") or []:
            if f not in base_facts and f not in have:
                new.setdefault("facts", []).append(f)
    return new


def sync(slug: str, *, wait_s: float = 30.0) -> dict:
    """Bring the brief and ``<repo>/.aiforge/memory/MEMORY.md`` into step.

    An edited repo file is imported first, then the brief is written back out.
    A repo that cannot be written (read-only mount) is recorded as such and the
    brief simply stays in the memory store. Never raises."""
    ent = entry(slug)
    if not ent:
        return {"ok": False, "error": "not a registered project"}
    # ``wait_s`` bounds the wait for another sync in flight: the caller on a
    # chat turn passes a short one and simply skips, the next sync catches up.
    if not _REG_LOCK.acquire(timeout=max(0.0, wait_s)):
        return {"ok": False, "error": "busy", "skipped": True}
    try:
        return _sync_locked(slug, ent)
    except Exception as exc:  # noqa: BLE001 — a mirror must never break a turn
        _log.debug("projects: sync %s failed: %s", slug, exc)
        return {"ok": False, "error": str(exc)}
    finally:
        _REG_LOCK.release()


def _sync_locked(slug: str, ent: dict) -> dict:
    rpath = repo_memory_path(ent["path"])
    repo_txt = _read(rpath)
    repo_body = (repo_txt or "").strip()
    brief = _brief_body(slug)
    imported = False
    if repo_body and _sha(repo_body) != ent.get("repo_sha"):
        brief_changed = bool(brief) and _sha(brief) != ent.get("brief_sha")
        if repo_body != brief:
            _write_brief(slug, ent["key"],
                         _merge_repo_edit(slug, repo_body, brief_changed))
            brief = _brief_body(slug)
            imported = True
    writable = ent.get("writable", True)
    exported = False
    if brief and brief != repo_body:
        try:
            rpath.parent.mkdir(parents=True, exist_ok=True)
            _atomic.write_text(str(rpath), brief + "\n")
            writable, exported, repo_body = True, True, brief
        except OSError as exc:
            writable = False
            _log.info("projects: %s not writable (%s) — brief stays in the "
                      "memory store", rpath, exc)
    if brief:
        _atomic.write_text(str(_base_path(slug)), brief + "\n")
    _update_entry(slug, repo_sha=_sha(repo_body), brief_sha=_sha(brief),
                  writable=writable, synced_at=time.time())
    return {"ok": True, "imported": imported, "exported": exported,
            "writable": writable}


def sync_for_repo(repo: "str | None", *, wait_s: float = 30.0) -> None:
    """Sync the project that owns memory key ``repo``, if it is one. The hook
    the write path calls after it changes a brief."""
    if not repo:
        return
    try:
        slug = slug_for(repo)
        if entry(slug):
            sync(slug, wait_s=wait_s)
    except Exception:  # noqa: BLE001
        pass


def open_project(path: str) -> "dict | None":
    """Register + sync a project folder, and start the one-time read of its
    instruction files. Called when a chat is opened on it."""
    ent = register(path)
    if not ent:
        return None
    sync(ent["slug"])
    ingest_instructions_async(ent["slug"])
    return entry(ent["slug"])


# ── first open: read the repo's own instruction files once ───────────────────

def _changed_instruction_files(ent: dict) -> "tuple[list[str], dict]":
    seen = dict(ent.get("ingested") or {})
    changed: list[str] = []
    for name in _INSTRUCTION_NAMES:
        p = Path(ent["path"]) / name
        txt = _read(p) if p.is_file() else None
        if txt is None:
            continue
        h = _sha(txt)
        if seen.get(name) != h:
            changed.append(str(p))
            seen[name] = h
    return changed, seen


def ingest_instructions(slug: str) -> dict:
    """Capture CLAUDE.md / AGENTS.md / GEMINI.md / .cursorrules into project
    memory. A file is read again only when its content hash changed."""
    ent = entry(slug)
    if not ent:
        return {"ok": False, "error": "not a registered project"}
    changed, seen = _changed_instruction_files(ent)
    if not changed:
        return {"ok": True, "files": 0}
    from aiforge_core.memory import instructions_ingest
    res = instructions_ingest.ingest_instruction_files(changed, compact=False)
    _update_entry(slug, ingested=seen)
    sync(slug)
    return {"ok": bool(res.get("ok")), "files": len(changed),
            "captured": res.get("captured", 0)}


def ingest_instructions_async(slug: str) -> None:
    if os.environ.get("AIFORGE_PROJECT_INGEST", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return
    ent = entry(slug)
    if not ent or not _changed_instruction_files(ent)[0]:
        return
    threading.Thread(target=lambda: _quiet(ingest_instructions, slug),
                     name=f"project-ingest-{slug}", daemon=True).start()


def _quiet(fn, *args):
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001
        _log.debug("projects: %s failed: %s", getattr(fn, "__name__", fn), exc)
        return None


# ── stale facts ──────────────────────────────────────────────────────────────

_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
              "__pycache__", "target", ".idea", ".gradle", "vendor",
              ".worktrees"}
_MAX_INDEX_FILES = 60000
_CODE_EXT = (r"py|java|kt|js|jsx|ts|tsx|go|rs|rb|php|c|h|cpp|hpp|cs|sql|sh|"
             r"bash|yaml|yml|json|toml|ini|xml|md|txt|csv|proto|gradle|tf|"
             r"html|css|scss|vue|swift")
# A file reference: an optional directory part and a name with a code
# extension. A leading ``/`` or ``~`` is captured so absolute paths can be told
# apart from repo-relative ones.
_REF_RE = re.compile(
    rf"(?<![\w/.:-])(~?/?(?:[\w.-]+/)*[\w][\w.-]*\.(?:{_CODE_EXT}))(?![\w/-])",
    re.IGNORECASE)
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)


def _file_index(project_path: str) -> "tuple[set[str], set[str], bool]":
    """``(relative paths, basenames, complete)`` for the project's files."""
    rels: set[str] = set()
    names: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(project_path):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        rel_dir = os.path.relpath(dirpath, project_path)
        for fn in filenames:
            rel = fn if rel_dir == "." else os.path.join(rel_dir, fn)
            rels.add(rel.replace(os.sep, "/").lower())
            names.add(fn.lower())
            if len(rels) >= _MAX_INDEX_FILES:
                return rels, names, False
    return rels, names, True


def _file_refs(fact: str) -> list[str]:
    text = _URL_RE.sub(" ", fact or "")
    return [m.group(1) for m in _REF_RE.finditer(text)]


def _ref_exists(ref: str, project_path: str, rels: set, names: set) -> "bool | None":
    """True/False when the reference can be judged against this repo, None when
    it points outside it (an absolute or home path elsewhere)."""
    if ref.startswith(("~", "/")):
        full = os.path.realpath(os.path.expanduser(ref))
        base = os.path.realpath(project_path)
        if full != base and not full.startswith(base + os.sep):
            return None
        return os.path.exists(full)
    low = ref.lstrip("./").replace(os.sep, "/").lower()
    if "/" not in low:
        return low in names
    return low in rels or any(r.endswith("/" + low) for r in rels)


def find_stale(facts: list, project_path: str) -> list[dict]:
    """Facts that name files, none of which exist in the repo any more.

    Conservative: a fact with no file reference, one that points outside the
    repo, or one where ANY named file still exists is left alone."""
    if not facts or not os.path.isdir(project_path):
        return []
    rels, names, complete = _file_index(project_path)
    if not complete:
        return []          # too big to be sure a file is gone
    out: list[dict] = []
    for f in facts:
        verdicts = [_ref_exists(r, project_path, rels, names)
                    for r in _file_refs(str(f))]
        judged = [v for v in verdicts if v is not None]
        if judged and len(judged) == len(verdicts) and not any(judged):
            out.append({"fact": str(f),
                        "missing": sorted(set(_file_refs(str(f))))})
    return out


def _stale_path(slug: str) -> Path:
    return _state_dir() / f"{slug}.stale.json"


def stale_list(slug: str) -> list[dict]:
    try:
        d = json.loads(_stale_path(slug).read_text(encoding="utf-8"))
        return d if isinstance(d, list) else []
    except Exception:  # noqa: BLE001
        return []


def _save_stale(slug: str, rows: list[dict]) -> None:
    _atomic.write_text(str(_stale_path(slug)), json.dumps(rows, indent=2))


def _unindex(facts: list, key: str) -> None:
    from aiforge_core.memory.md_store import _render
    try:
        from aiforge_core.memory import backend_select, sqlite_memory
        if not backend_select.embedded():
            return
        for f in facts:
            body = _render._fact_body(f)
            if len(body) >= 12:
                sqlite_memory.delete_by_text_contains(
                    body, repo=key, exclude_kind="knowledge")
    except Exception:  # noqa: BLE001
        pass


def sweep_stale(slug: str) -> dict:
    """Move facts about files that no longer exist out of the brief and into
    the project's stale list. They stop being injected and recalled; the user
    can restore or delete them."""
    ent = entry(slug)
    if not ent:
        return {"ok": False, "error": "not a registered project"}
    sec = _current_sections(slug)
    found = find_stale(sec.get("facts") or [], ent["path"])
    if not found:
        return {"ok": True, "moved": 0}
    gone = {s["fact"] for s in found}
    sec["facts"] = [f for f in sec["facts"] if f not in gone]
    _write_brief(slug, ent["key"], sec)
    _unindex(list(gone), ent["key"])
    known = {r.get("fact") for r in stale_list(slug)}
    now = time.time()
    _save_stale(slug, stale_list(slug) + [
        {**s, "at": now} for s in found if s["fact"] not in known])
    sync(slug)
    return {"ok": True, "moved": len(found)}


def stale_restore(slug: str, fact: str) -> dict:
    ent = entry(slug)
    rows = stale_list(slug)
    if not ent or not any(r.get("fact") == fact for r in rows):
        return {"ok": False, "error": "no such stale fact"}
    sec = _current_sections(slug)
    if fact not in (sec.get("facts") or []):
        sec.setdefault("facts", []).append(fact)
        _write_brief(slug, ent["key"], sec)
    _save_stale(slug, [r for r in rows if r.get("fact") != fact])
    sync(slug)
    return {"ok": True}


def stale_delete(slug: str, fact: str) -> dict:
    rows = stale_list(slug)
    keep = [r for r in rows if r.get("fact") != fact]
    if len(keep) == len(rows):
        return {"ok": False, "error": "no such stale fact"}
    _save_stale(slug, keep)
    return {"ok": True}


# ── size cap + compaction ────────────────────────────────────────────────────

def _knowledge(slug: str) -> str:
    from aiforge_core.runtime import work_notes
    body = _brief_body(slug)
    return work_notes.knowledge_text(body) if body else ""


def _archive_brief(slug: str, reason: str) -> "str | None":
    """Copy the brief aside before it is rewritten. Nothing is deleted."""
    from aiforge_core.memory import md_store
    src = md_store.brief_path(slug)
    if not src.is_file():
        return None
    dst = md_store.memory_dir() / "archive" / "projects" / slug
    dst.mkdir(parents=True, exist_ok=True)
    out = dst / f"{time.strftime('%Y%m%dT%H%M%S')}-{reason}.md"
    shutil.copy2(src, out)
    return str(out)


def compact(slug: str, *, force: bool = False) -> dict:
    """Keep one project's memory in shape: sweep stale facts, then, when the
    brief is over its cap (or ``force``), fold it into a shorter one with the
    model. The previous version is archived. If no model answers, the brief is
    left exactly as it was."""
    ent = entry(slug)
    if not ent:
        return {"ok": False, "error": "not a registered project"}
    sync(slug)
    out: dict = {"ok": True, "stale": sweep_stale(slug).get("moved", 0)}
    text = _knowledge(slug)
    out["chars_before"] = len(text)
    if not text or (not force and len(text) <= memory_cap()):
        out["compacted"] = False
        return out
    from aiforge_core.memory.md_store import _compact_summarize
    try:
        summary = _compact_summarize._summarize_notes([text], _compact_role())
    except Exception as exc:  # noqa: BLE001
        summary = None
        out["error"] = f"summarize failed: {exc}"
    if not summary or not summary.strip():
        out["compacted"] = False
        out.setdefault("error", "no model available — brief left unchanged")
        return out
    out["archived"] = _archive_brief(slug, "compact")
    sec = _current_sections(slug)
    # A model writes its own "## " headings; left as they are, the brief's
    # parser would read them as sections of the envelope.
    from aiforge_core.memory.md_store import _compact_sweep
    sec["facts"], sec["learnings"] = [], []
    sec["body"] = _compact_sweep._demote_headings(summary.strip())
    _write_brief(slug, ent["key"], sec)
    _update_entry(slug, compacted_at=time.time())
    sync(slug)
    out["compacted"] = True
    out["chars_after"] = len(_knowledge(slug))
    return out


def compact_over_cap() -> dict:
    """The periodic pass: every registered project, stale sweep always,
    compaction only past the cap."""
    done: dict = {}
    for slug in list(registered()):
        done[slug] = _quiet(compact, slug)
    return done


# ── edit / promote / forget ──────────────────────────────────────────────────

def read(slug: str) -> "dict | None":
    ent = entry(slug)
    if not ent:
        return None
    sync(slug)
    ent = entry(slug) or ent
    body = _brief_body(slug)
    return {
        "name": ent.get("name"), "key": ent.get("key"), "slug": slug,
        "path": ent.get("path"), "text": body,
        "chars": len(_knowledge(slug)), "cap": memory_cap(),
        "writable": ent.get("writable", True),
        "repo_file": str(repo_memory_path(ent["path"])),
        "compacted_at": ent.get("compacted_at"),
        "synced_at": ent.get("synced_at"),
        "stale": stale_list(slug),
    }


def save(slug: str, text: str) -> dict:
    """Replace the brief with hand-edited markdown."""
    ent = entry(slug)
    if not ent:
        return {"ok": False, "error": "not a registered project"}
    from aiforge_core.memory.md_store import _render
    before = _current_sections(slug)
    after = _render._parse_brief(text or "")
    after["sources"] = before.get("sources") or []
    after["tags"] = after.get("tags") or before.get("tags") or []
    _archive_brief(slug, "edit")
    _write_brief(slug, ent["key"], after)
    kept = set(after.get("facts") or [])
    _unindex([f for f in before.get("facts") or [] if f not in kept], ent["key"])
    sync(slug)
    return {"ok": True, "chars": len(_knowledge(slug))}


def _lines(text: str) -> list[str]:
    out = []
    for ln in (text or "").splitlines():
        s = re.sub(r"^[-*]\s+", "", ln.strip())
        if s:
            out.append(s)
    return out


def promote(slug: str, text: str) -> dict:
    """Move facts from the project brief into global memory."""
    ent = entry(slug)
    if not ent:
        return {"ok": False, "error": "not a registered project"}
    wanted = _lines(text)
    if not wanted:
        return {"ok": False, "error": "nothing selected"}
    from aiforge_core.memory import md_store
    from aiforge_core.memory.md_store import _render
    sec = _current_sections(slug)
    moved: list[str] = []
    for w in wanted:
        hit = next((f for f in sec.get("facts") or []
                    if f == w or _render._fact_body(f) == w), None)
        res = md_store.capture("learning", w, repo=None, classify=False,
                               source=f"promote:{slug}")
        if isinstance(res, dict) and res.get("skipped"):
            _render._brief_upsert("shared", w)
        if hit is not None:
            sec["facts"].remove(hit)
        moved.append(w)
    _write_brief(slug, ent["key"], sec)
    _unindex(moved, ent["key"])
    sync(slug)
    return {"ok": True, "moved": len(moved)}


def forget(slug: str) -> dict:
    """Forget a project: archive its brief, drop its index rows and its mirror
    in the repo, and unregister it. The archive copy stays."""
    ent = entry(slug)
    if not ent:
        return {"ok": False, "error": "not a registered project"}
    from aiforge_core.memory import md_store
    archived = _archive_brief(slug, "forget")
    removed = 0
    try:
        md_store.delete_file(f"compacted-{slug}")
        from aiforge_core.memory import backend_select, sqlite_memory
        if backend_select.embedded():
            removed = sqlite_memory.delete_by_repo(ent["key"])
    except Exception as exc:  # noqa: BLE001
        _log.debug("projects: forget %s index cleanup failed: %s", slug, exc)
    for p in (repo_memory_path(ent["path"]), _base_path(slug), _stale_path(slug)):
        try:
            p.unlink()
        except OSError:
            pass
    with _REG_LOCK:
        reg = _load_registry()
        reg.pop(slug, None)
        _save_registry(reg)
    return {"ok": True, "archived": archived, "rows_removed": removed}


def summary(folder: dict) -> dict:
    """One folder row plus its memory numbers, for the Projects page."""
    key = key_for(folder["path"])
    slug = slug_for(key)
    ent = entry(slug) or {}
    chars = len(_knowledge(slug))
    return {**folder, "key": key, "slug": slug,
            "registered": bool(ent),
            "memory_chars": chars, "memory_cap": memory_cap(),
            "stale": len(stale_list(slug)) if ent else 0,
            "writable": ent.get("writable", True)}


__all__ = [
    "GENERAL", "root", "list_folders", "resolve", "project_of", "key_for",
    "slug_for", "register", "registered", "entry", "open_project", "sync",
    "sync_for_repo", "ingest_instructions", "find_stale", "sweep_stale",
    "stale_list", "stale_restore", "stale_delete", "compact",
    "compact_over_cap", "read", "save", "promote", "forget", "summary",
    "memory_cap", "repo_memory_path",
]
