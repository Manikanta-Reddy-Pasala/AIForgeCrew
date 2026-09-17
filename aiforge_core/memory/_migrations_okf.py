"""One-time migrations to the OKF layout: archiving the old DAG folder,
frontmatter, the renamed folder and peer files, and the markers that record them."""
from __future__ import annotations

import json
import os

from aiforge_core.config import _atomic


def _pkg():
    """The parent module, looked up when called, so a replaced name there is the one
    used here."""
    import aiforge_core.memory.migrations as package
    return package


def _archive_okr_dag_folder() -> dict:
    """Move a live ``<memory>/okr/`` node-graph folder to a sibling
    ``<memory>/../memory-archive/okr[-N]/`` (kept, reversible). Idempotent —
    a no-op once the live dir is gone. Never raises."""
    import shutil
    try:
        from aiforge_core.memory.md_store import memory_dir
        src = memory_dir() / "okr"
        # A folder holding ONLY the migration bookkeeping marker (.migrations.json,
        # which _save_marker rewrites into okf_root after each archive) is NOT real
        # DAG data — treat it as empty so we don't re-archive a marker-only folder
        # into okr-1, okr-2… on every restart.
        if not src.is_dir():
            return {"skipped": "no live okr/ folder"}
        real = [p for p in src.iterdir() if p.name != _pkg()._MIGRATIONS_JSON]
        if not real:
            return {"skipped": "only migration marker — nothing to archive"}
        arch_root = memory_dir().parent / "memory-archive"
        arch_root.mkdir(parents=True, exist_ok=True)
        dest = arch_root / "okr"
        n = 1
        while dest.exists():                    # never clobber an earlier archive
            dest = arch_root / f"okr-{n}"
            n += 1
        shutil.move(str(src), str(dest))
        _pkg().log.info("archived stale okr/ DAG folder → %s", dest)
        return {"ok": True, "archived_to": str(dest)}
    except Exception as exc:  # noqa: BLE001 — archiving must never break startup
        return {"ok": False, "error": str(exc)}


# Legacy frontmatter key → OKF name. created_at folds into timestamp too.
_OKF_KEY_RENAMES = (("kind", "type"), ("source_url", "resource"),
                    ("updated_at", "timestamp"), ("created_at", "timestamp"))


def _split_frontmatter(text: str) -> tuple[str, str, str] | None:
    r"""``(opening, body, closing)`` of a YAML front-matter block, or None.

    Index arithmetic instead of `\A(---\s*\n)(.*?)(\n---\s*\n?)`: a lazy
    quantifier spanning a whole file is the denial-of-service shape a scanner
    asks about, and "find the closing marker" is a str.find.
    """
    if not text.startswith("---"):
        return None
    first_nl = text.find("\n")
    if first_nl == -1 or text[3:first_nl].strip():
        return None                       # "---extra" is not an opener
    close = text.find("\n---", first_nl)
    if close == -1:
        return None
    end = text.find("\n", close + 1)
    tail_end = end + 1 if end != -1 else len(text)
    if text[close + 4:tail_end].strip():
        return None                       # the closing line carries content
    return text[:first_nl + 1], text[first_nl + 1:close], text[close:tail_end]


def _rewrite_file_frontmatter_to_okf(path) -> bool:
    """Rename legacy frontmatter keys → OKF names in ONE md file's frontmatter
    block (body untouched). Idempotent: a key is renamed only when its OKF name
    isn't already present, so re-runs and mixed files are safe. Atomic write.
    Returns True iff the file changed. Never raises."""
    import re
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return False
    parsed = _split_frontmatter(text)
    if parsed is None:
        return False
    head, fm, tail = parsed
    lines = fm.split("\n")
    present = {ln.split(":", 1)[0].strip() for ln in lines if ":" in ln}
    changed = False
    out_lines: list[str] = []
    for ln in lines:
        renamed = False
        for legacy, okf in _OKF_KEY_RENAMES:
            mm = re.match(rf"^(\s*){re.escape(legacy)}(\s*:.*)$", ln)
            if mm and okf not in present:
                out_lines.append(f"{mm.group(1)}{okf}{mm.group(2)}")
                present.add(okf)
                changed = True
                renamed = True
                break
        if not renamed:
            out_lines.append(ln)
    if not changed:
        return False
    # head + body + closing marker, then everything after the block. The old
    # form used the match object's end(); the split helper returns the three
    # pieces, so the remainder is simply what their lengths do not cover.
    consumed = len(head) + len(fm) + len(tail)
    new_text = head + "\n".join(out_lines) + tail + text[consumed:]
    try:
        _atomic.write_text(path, new_text)
    except OSError:
        return False
    return True


def _migrate_frontmatter_to_okf() -> dict:
    """Rewrite legacy frontmatter keys (kind/source_url/updated_at/created_at)
    to OKF names across EVERY memory Markdown file — briefs (``compacted/``),
    raw captures (``captures/``), session notes, rule books, and the ``okf/``
    node bundle. Reserved OKF files (index.md/log.md) and the historical
    ``archive/`` snapshots are skipped. Idempotent + soft-fail — brings every
    pre-OKF on-disk file up to spec so a Google OKF reader consumes it directly."""
    try:
        from aiforge_core.memory.md_store import memory_dir
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    changed = 0
    scanned = 0
    root = memory_dir()
    if not root.is_dir():
        return {"ok": True, "rewritten": 0, "scanned": 0}
    for p in root.rglob("*.md"):
        # skip reserved OKF nav/audit files and archived historical snapshots
        if p.name in ("index.md", "log.md"):
            continue
        parts = set(p.relative_to(root).parts)
        if "archive" in parts or "memory-archive" in parts:
            continue
        scanned += 1
        try:
            if _pkg()._rewrite_file_frontmatter_to_okf(p):
                changed += 1
        except Exception:  # noqa: BLE001 — one bad file never blocks the rest
            continue
    return {"ok": True, "rewritten": changed, "scanned": scanned}


def migrate_okf_format() -> dict:
    """Explicit, standalone OKF format migration (the ``./run.sh --migrate-okf``
    entry): rename a legacy ``okr/`` node bundle → ``okf/`` and rewrite every
    memory Markdown file's frontmatter to OKF names. Idempotent + soft-fail.
    run.sh calls this on every start so old-format files always converge to OKF."""
    out = {"dir_rename": _pkg()._rename_okr_dir_to_okf(),
           "frontmatter": _pkg()._migrate_frontmatter_to_okf()}
    _pkg().log.info("migrate_okf_format: %s", out)
    return out


def _rename_okr_dir_to_okf() -> dict:
    """When the DAG is ON, rename a legacy ``<memory>/okr/`` node bundle to the
    OKF folder name ``<memory>/okf/`` so existing nodes are found at the new
    root. No-op if okr/ is absent or okf/ already exists. Never raises."""
    import shutil
    try:
        from aiforge_core.memory.md_store import memory_dir
        src = memory_dir() / "okr"
        dst = memory_dir() / "okf"
        if not src.is_dir():
            return {"skipped": "no legacy okr/ folder"}
        if dst.exists():
            # okf/ is already the live bundle — an okr/ holding only stale
            # bookkeeping (.migrations.json) is orphaned; remove it so the tree
            # is clean. Anything else stays put (don't clobber real data).
            leftover = [p for p in src.iterdir() if p.name != _pkg()._MIGRATIONS_JSON]
            if not leftover:
                shutil.rmtree(str(src), ignore_errors=True)
                return {"ok": True, "removed_stale_marker": str(src)}
            return {"skipped": "okf/ exists and okr/ has data — left in place"}
        shutil.move(str(src), str(dst))
        _pkg().log.info("renamed legacy okr/ node bundle → okf/")
        return {"ok": True, "moved_to": str(dst)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _drain_peer_files(src, _paths, shutil) -> "tuple[int, int]":
    """Move each foreign node file out of ``src`` into the peers inbox. Returns
    ``(moved, kept)`` — a file already at the destination is kept in place (the
    newer layout wins, nothing is lost)."""
    moved = kept = 0
    for f in sorted(src.rglob("*")):
        if not f.is_file():
            continue
        dest = _paths.peers_root() / f.relative_to(src)
        if dest.exists():
            kept += 1                   # destination wins; nothing is lost
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
        moved += 1
    return moved, kept


def _move_okf_peers_to_inbox() -> dict:
    """Move foreign OKF nodes out of ``okf/peers/<origin>/`` into the top-level
    ``peers/<origin>/`` inbox.

    ``okf/`` now means "knowledge this machine authored" — the compaction source
    and the only thing this peer contributes to the mesh — so other peers' raw
    nodes must not sit inside it. Idempotent; never clobbers a file already at the
    destination; never raises (a node must boot even when its memory tree is odd).
    """
    import shutil
    try:
        from aiforge_core.memory.sync import paths as _paths
        src = _paths.legacy_peers_dir()
        if not src.is_dir():
            return {"ok": True, "skipped": "no legacy okf/peers/ folder"}
        moved, kept = _drain_peer_files(src, _paths, shutil)
        for d in sorted(src.rglob("*"), reverse=True):
            if d.is_dir():
                _rmdir_if_empty(d)
        _rmdir_if_empty(src)                # gone entirely once fully drained
        _pkg().log.info("moved %s foreign okf/peers/ node(s) → peers/ (%s kept in place)",
                 moved, kept)
        return {"ok": True, "moved": moved, "kept_at_destination": kept}
    except Exception as exc:  # noqa: BLE001 — a migration must never block startup
        return {"ok": False, "error": str(exc)}


def _rmdir_if_empty(d) -> None:
    import contextlib
    with contextlib.suppress(OSError):   # not empty, or already gone — both fine
        d.rmdir()


def _discover_repos() -> list:
    """Best-effort GENERIC repo list for classification — no hardcoded paths.
    Sources: AIFORGE_REPOS_ROOT (explicit), sibling git repos of the running
    checkout (repos are usually cloned side by side), and any repo context
    folders under the workspace. Returns real repo NAMES."""
    import subprocess
    repos: set = set()
    roots: list = []
    env_root = os.environ.get("AIFORGE_REPOS_ROOT", "").strip()
    if env_root:
        roots.append(env_root)
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=5)
        if top.returncode == 0 and top.stdout.strip():
            roots.append(os.path.dirname(top.stdout.strip()))   # siblings
    except Exception:  # noqa: BLE001
        pass
    for rt in roots:
        try:
            for name in os.listdir(rt):
                if os.path.isdir(os.path.join(rt, name, ".git")):
                    repos.add(name)
        except OSError:
            continue
    return sorted(repos)


def _marker_path() -> str:
    from aiforge_core.memory.okf import store as _store
    return os.path.join(_store.okf_root(), _pkg()._MIGRATIONS_JSON)


def _load_marker() -> dict:
    try:
        with open(_marker_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_marker(done: dict) -> None:
    import contextlib
    with contextlib.suppress(OSError):
        _atomic.write_text(_marker_path(), json.dumps(done))

