from __future__ import annotations

from ._shared import _coerce_int


def _t_codegraph_query(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import codegraph
    return codegraph.codegraph_query(args, cwd)


def _t_codegraph_callers(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import codegraph
    return codegraph.codegraph_callers(args, cwd)


def _t_codegraph_callees(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import codegraph
    return codegraph.codegraph_callees(args, cwd)


def _t_codegraph_impact(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import codegraph
    return codegraph.codegraph_impact(args, cwd)


def _t_codegraph_explore(args: dict, cwd: str) -> dict:
    from aiforge_core.runtime.tools import codegraph
    return codegraph.codegraph_explore(args, cwd)


def _t_read_lines(args: dict, cwd: str) -> dict:
    import os as _os
    path = str(args.get("path") or "")
    fp = path if _os.path.isabs(path) else _os.path.join(cwd or ".", path)
    try:
        with open(fp, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except FileNotFoundError:
        return {"ok": False, "error": f"not found: {path}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    n = len(lines)
    s = max(1, _coerce_int(args.get("start"), 1) or 1)
    e = n if not args.get("end") else min(_coerce_int(args.get("end"), n), n)
    if s > n:
        return {"ok": True, "path": path, "total_lines": n, "text": ""}
    return {"ok": True, "path": path, "start": s, "end": e, "total_lines": n,
            "text": "".join(lines[s - 1:e][:5000])[:60000]}


def _resolve_doc(path: str, cwd: str) -> str | None:
    """Locate a document to summarise — an absolute path, a path relative to the
    chat workspace, or (most common) an attachment by name in the session's
    media folder (``<cwd>/.aiforge/media``). Fuzzy basename match as a fallback."""
    import os as _os
    base = _os.path.basename(path)
    media = _os.path.join(cwd or ".", ".aiforge", "media")
    for c in (path, _os.path.join(cwd or ".", path), _os.path.join(media, base)):
        if c and _os.path.isfile(c):
            return c
    if _os.path.isdir(media):
        low = base.lower()
        for f in sorted(_os.listdir(media)):
            if low in f.lower():
                return _os.path.join(media, f)
    return None


def _t_summarize_doc(args: dict, cwd: str) -> dict:
    """Summarise an attached/loaded document (pdf / docx / xlsx). Optional
    ``pages`` (e.g. "10-20", "3,5,7-9") summarises ONLY those pages/sections via
    the map-reduce summariser, so a 400-page report can be read section by
    section. No ``pages`` → the whole document."""
    import os as _os
    path = str(args.get("path") or args.get("file") or args.get("filename") or "").strip()
    pages = str(args.get("pages") or "").strip() or None
    role = str(args.get("role") or "chat").strip() or "chat"
    if not path:
        return {"ok": False, "error": "need a file path/name"}
    fp = _resolve_doc(path, cwd)
    if not fp:
        return {"ok": False, "error": f"file not found: {path}"}
    from aiforge_core.runtime import doc_extract, doc_summarize
    name = _os.path.basename(fp)
    _pages, kind = doc_extract.paginate(fp, "")
    total = len(_pages)
    if kind == "approx":
        note = ("page numbers are APPROXIMATE (char-estimated — this file has no "
                "reliable page breaks, so they may not match a Word viewer's "
                "printed pages)")
    elif kind == "word":
        note = ("total page count is Word's own; the text mapped to each page is "
                "an approximate slice, so a specific page's content may be "
                "slightly off")
    else:
        note = None
    # Out-of-range page request → don't fail silently; tell the model the real
    # page count so it can relay that instead of retrying blindly.
    if pages:
        idx = doc_extract.parse_page_spec(pages, total)
        if not idx:
            return {"ok": False, "file": name, "page_count": total,
                    "pagination": kind, "note": note,
                    "error": f"requested pages {pages!r} are out of range — "
                             f"{name} has {total} page(s)"
                             + (f" ({note})" if note else "")}
    summary = doc_summarize.summarize_document(fp, role=role, pages=pages)
    if not summary:
        scope = f" for pages {pages}" if pages else ""
        return {"ok": False, "file": name, "page_count": total,
                "error": f"nothing readable{scope} in {name}"}
    out = {"ok": True, "file": name, "page_count": total, "pagination": kind,
           "pages": pages or "all", "summary": summary}
    if note:
        out["note"] = note
    return out


def _t_rename_symbol(args: dict, cwd: str) -> dict:
    import os as _os
    import re as _re
    name = str(args.get("name") or "")
    new = str(args.get("new_name") or "")
    if not name or not new:
        return {"ok": False, "error": "need 'name' and 'new_name'"}
    dry = args.get("dry_run", True)
    base = str(args.get("path") or ".")
    root_p = base if _os.path.isabs(base) else _os.path.join(cwd or ".", base)
    pat = _re.compile(r"\b" + _re.escape(name) + r"\b")
    _EXT = (".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs", ".c",
            ".cpp", ".h", ".cs", ".rb", ".php", ".kt", ".scala", ".swift")
    hits, changed = [], 0
    for dp, dn, fns in _os.walk(root_p):
        dn[:] = [d for d in dn if d not in (".git", "node_modules", ".venv",
                 "venv", "dist", "build", "__pycache__")]
        for fn in fns:
            if not fn.endswith(_EXT):
                continue
            fpath = _os.path.join(dp, fn)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    txt = fh.read()
            except Exception:  # noqa: BLE001
                continue
            c = len(pat.findall(txt))
            if not c:
                continue
            hits.append({"file": _os.path.relpath(fpath, cwd or "."),
                         "occurrences": c})
            if not dry:
                try:
                    with open(fpath, "w", encoding="utf-8") as fh:
                        fh.write(pat.sub(new, txt))
                    changed += c
                except Exception:  # noqa: BLE001
                    pass
    return {"ok": True, "name": name, "new_name": new, "dry_run": bool(dry),
            "files": hits, "total_occurrences": sum(h["occurrences"] for h in hits),
            "applied": (0 if dry else changed)}
