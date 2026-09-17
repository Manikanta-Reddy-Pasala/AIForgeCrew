"""Which files an ingest reads: walking the tree, counting indexable files,
validating the target path and reading sources."""
from __future__ import annotations

import os
from pathlib import Path


def _pkg():
    """``memory_ingest``, the module this code was split from, looked up on each call.

    Only names read through here follow a patch on ``memory_ingest``; patch any other
    name on this module."""
    import aiforge_core.runtime.memory_ingest as package
    return package


def _iter_files(root: Path, exts: set[str]):
    noise = _pkg()._NOISE_DIRS
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in noise
                       and not d.startswith(".")]
        for fn in filenames:
            if Path(fn).suffix.lower() in exts:
                yield Path(dirpath) / fn


def _count_indexable(p: Path, sample: list) -> tuple[int, int]:
    """Capped counts of code and doc files under ``p``; fills ``sample`` with up
    to 8 relative code-file paths. Capped at 5000 each — a clear signal, not a
    full census, so a huge tree doesn't stall the pre-flight."""
    pkg = _pkg()
    code = doc = 0
    for f in _iter_files(p, pkg._CODE_EXT):
        code += 1
        if len(sample) < 8:
            sample.append(str(f.relative_to(p)))
        if code >= 5000:
            break
    for _ in _iter_files(p, pkg._ALL_DOC_EXT):
        doc += 1
        if doc >= 5000:
            break
    return code, doc


def _validate_path_target(p: Path, resolved: str, out: dict) -> str | None:
    """Fill the exists/is_dir/readable flags; return an error message when the
    target cannot be indexed, or None when it is a readable directory."""
    out["exists"] = p.exists()
    if not p.exists():
        return (f"path does not exist (resolved to {resolved}). Use an ABSOLUTE "
                f"path to the repo root; a relative path resolves against the "
                f"api's working directory.")
    out["is_dir"] = p.is_dir()
    if not p.is_dir():
        return f"not a directory: {resolved}"
    out["readable"] = os.access(str(p), os.R_OK)
    return None


def validate_path(location: str) -> dict:
    """Pre-flight a repo/dir path BEFORE indexing so the user can see whether
    the process can actually reach it. Returns the RESOLVED absolute path (what
    the walk will use), whether it exists / is a dir / is readable, and how many
    code + doc files are under it — so a wrong/empty/relative path is caught up
    front instead of silently indexing 0 units. Never raises."""
    out = {"input": location, "ok": False, "resolved": "", "exists": False,
           "is_dir": False, "readable": False, "code_files": 0, "doc_files": 0,
           "sample": [], "message": ""}
    try:
        loc = (location or "").strip()
        if not loc:
            out["message"] = "empty path"
            return out
        p = Path(loc).expanduser()
        out["resolved"] = str(p.resolve()) if p.exists() else str(p.absolute())
        err = _validate_path_target(p, out["resolved"], out)
        if err:
            out["message"] = err
            return out
        code, doc = _count_indexable(p, out["sample"])
        out["code_files"], out["doc_files"] = code, doc
        if code == 0 and doc == 0:
            out["message"] = (f"0 indexable files under {out['resolved']} — the "
                              f"directory is empty from the api's view. On a "
                              f"HYBRID/host run give the ABSOLUTE host path to the "
                              f"repo root (the dir with src/ or pom.xml); on Docker "
                              f"mount it under /workspace.")
        else:
            out["ok"] = True
            out["message"] = (f"OK — {code} code + {doc} doc files under "
                              f"{out['resolved']}")
    except Exception as exc:  # noqa: BLE001
        out["message"] = f"validation error: {exc}"
    return out


def _read_source(f: Path) -> "str | None":
    """Read a file to text. Binary docs (pdf/docx) go through
    ``chat_media.extract_text`` (pypdf / python-docx); text files are read
    straight off disk. Returns None on any failure (soft-skip)."""
    pkg = _pkg()
    ext = f.suffix.lower()
    if ext in pkg._BINARY_DOC_EXT:
        try:
            from aiforge_core.runtime import chat_media
            text = chat_media.extract_text(str(f))
        except Exception:  # noqa: BLE001 — missing dep / corrupt file
            return None
        return text or None
    try:
        if f.stat().st_size > pkg._MAX_FILE:
            return None
        return f.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
