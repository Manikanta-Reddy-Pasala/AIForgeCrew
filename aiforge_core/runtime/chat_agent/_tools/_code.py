from __future__ import annotations



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


def _t_code_search(args: dict, cwd: str) -> dict:
    """Natural-language code search — embed+keyword, no codegraph binary."""
    from aiforge_core.runtime.tools import code_search as _cs
    return _cs.code_search(args, cwd)


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
    args, bad = _normalize_read_args(args)
    if bad:
        return {"ok": False, "path": path, "total_lines": n, "error": bad}
    ranges = _ranges(args.get("ranges"))
    if ranges:
        return _read_ranges(path, lines, ranges)
    s, e = _line_range(args, n)
    if s > n:
        return {"ok": True, "path": path, "total_lines": n, "text": ""}
    s, e, widened = _widen(s, e, n)
    out = {"ok": True, "path": path, "start": s, "end": e, "total_lines": n,
           "text": "".join(lines[s - 1:e][:5000])[:60000]}
    if widened:
        out["note"] = (f"widened to lines {s}-{e} of {n}: read a file once, "
                       "not in many small pieces")
    return out


#: Arguments read_lines understands (after aliases are folded in).
_READ_KEYS = frozenset({"path", "start", "end", "offset", "limit", "ranges", "full"})
#: Other names models use for the same thing.
_START_ALIASES = ("line_start", "start_line", "from", "first_line")
_END_ALIASES = ("line_end", "end_line", "to", "last_line", "offset_end")
#: Around a single line number given as the whole range.
_AROUND = 40


def _span(raw):
    """``(start, end)`` from ``[s, e]``, ``"s-e"`` / ``"s,e"`` / ``"s:e"`` /
    ``"s..e"``, or one number (a window around it). None when unusable."""
    if isinstance(raw, dict):
        s, e = _num(raw.get("start")), _num(raw.get("end"))
        return (s, e) if s and e and e >= s else None
    if isinstance(raw, (list, tuple)):
        if len(raw) == 2:
            s, e = _num(raw[0]), _num(raw[1])
            return (s, e) if s and e and e >= s else None
        if len(raw) == 1:
            return _span(raw[0])
        return None
    if isinstance(raw, str):
        import re as _re
        parts = [p for p in _re.split(r"\s*(?:-|,|:|\.\.|\bto\b)\s*", raw.strip()) if p]
        if len(parts) == 2:
            s, e = _num(parts[0]), _num(parts[1])
            return (s, e) if s and e and e >= s else None
        if len(parts) == 1:
            raw = parts[0]
        else:
            return None
    c = _num(raw)
    if c is None or c < 1:
        return None
    return max(1, c - _AROUND), c + _AROUND


def _normalize_read_args(args: dict) -> "tuple[dict, str]":
    """Fold the other names models use (``range``, ``lines``, ``line_start``…)
    into start/end, and refuse what is left over. Bench (11-11): 69 of 70
    reads sent ``{"range": [2179, 2350]}``; each came back as the file head,
    so the model never saw the lines and asked again."""
    a = dict(args or {})
    for key in ("range", "lines", "line_range"):
        if key in a:
            raw = a.pop(key)
            if (isinstance(raw, list) and len(raw) > 1
                    and all(isinstance(r, (list, tuple, str, dict)) for r in raw)):
                a.setdefault("ranges", raw)  # several spans under one name
                continue
            sp = _span(raw)
            if sp is None:
                return a, (f"read_lines: could not read {key}={raw!r}; "
                           "give start and end line numbers, e.g. "
                           '{"start": 120, "end": 180}')
            a.setdefault("start", sp[0])
            a.setdefault("end", sp[1])
    for names, target in ((_START_ALIASES, "start"), (_END_ALIASES, "end")):
        for k in names:
            if k in a:
                v = a.pop(k)
                a.setdefault(target, v)
    if isinstance(a.get("ranges"), list):
        fixed = []
        for r in a["ranges"]:
            sp = _span(r)
            fixed.append(list(sp) if sp else r)
        a["ranges"] = fixed
    extra = sorted(k for k in a if k not in _READ_KEYS)
    if extra:
        return a, (f"read_lines: unknown argument(s) {', '.join(extra)}; "
                   "use path with start/end (or offset/limit), or ranges "
                   "[[start, end], ...]")
    return a, ""


#: The most ranges one call reads, and the most text it returns.
_MAX_RANGES, _RANGES_CHARS = 8, 60000


def _ranges(raw) -> "list[tuple[int, int]]":
    """``[(start, end), …]`` from ``[[s, e], …]`` or ``[{"start", "end"}, …]``
    (offset/limit accepted too); [] when nothing usable was given."""
    if not isinstance(raw, list):
        return []
    out = []
    for r in raw[:_MAX_RANGES]:
        if isinstance(r, (list, tuple)) and len(r) == 2:
            r = {"start": r[0], "end": r[1]}
        if isinstance(r, dict):
            s, e = _line_range(r, 10 ** 9)
            if e != 10 ** 9 and e >= s:
                out.append((s, e))
    return out


def _read_ranges(path: str, lines: list, ranges: list) -> dict:
    """Several parts of one file in one call. Bench (attrs-inspect, 11-08):
    77 of 151 reads came right after another read of the same file, about 30
    lines each — a model call per slice."""
    n, parts, used = len(lines), [], 0
    for s, e in sorted(set(ranges)):
        if s > n:
            continue
        e = min(e, n)
        text = "".join(lines[s - 1:e])
        if used + len(text) > _RANGES_CHARS:
            text = text[:max(0, _RANGES_CHARS - used)]
        used += len(text)
        parts.append({"start": s, "end": e, "text": text})
        if used >= _RANGES_CHARS:
            break
    return {"ok": True, "path": path, "total_lines": n, "parts": parts}


def _num(v):
    """A line number from a tool argument: 1850, 1850.0 or "1850.0"."""
    try:
        return int(float(v)) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _line_range(args: dict, n: int) -> "tuple[int, int]":
    """``(start, end)`` from start/end, or the offset/limit pair OpenCode's
    read tool takes. A bench run sent ``{"end": 1850, "limit": 200}`` and
    ``{"offset": 1700, "end": 1750}`` in 28 of 97 reads; with only start/end
    read, each came back from line 1 (66k chars of core.py) and the model
    asked again."""
    s = _num(args.get("start"))
    if s is None:
        s = _num(args.get("offset"))
    e, limit = _num(args.get("end")), _num(args.get("limit"))
    if e and limit and limit > 0 and s is None:
        s = e - limit + 1
    elif not e and limit and limit > 0:
        e = (s or 1) + limit - 1
    s = max(1, s or 1)
    return s, (n if not e else min(e, n))


def _read_env(key: str, default: int) -> int:
    import os as _os
    try:
        return max(0, int(_os.environ.get(key, default)))
    except ValueError:
        return default


def _widen(s: int, e: int, n: int) -> "tuple[int, int, bool]":
    """A whole small file, or at least READ_MIN_LINES around the range asked.

    A benchmark run read files 40 lines at a time (median): models.py 15
    times, inventory.py 13 — a model call each. OpenCode reads a file whole
    (up to 2,000 lines). On big repos the gap was the read count: sqlglot,
    AIForge 216 reads (median 40 lines) in 612 steps vs OpenCode 88 in 429,
    same speed per step. But both runs with widening on did worse than the
    same build without it (shopkit fid8 32/60 vs 52; click-completion rerun
    6/37 vs 20/37): bigger reads filled the context and set off a prune every
    ~3 min, each a full prompt re-read. OFF by default; env
    AIFORGE_CHAT_READ_MIN_LINES / _WHOLE_LINES turn it on for tests."""
    whole, least = (_read_env("AIFORGE_CHAT_READ_WHOLE_LINES", 0),
                    _read_env("AIFORGE_CHAT_READ_MIN_LINES", 0))
    if whole and n <= whole:
        return 1, n, (s, e) != (1, n)
    if least and e - s + 1 < least:
        lo = max(1, s - 20)
        hi = min(n, lo + least - 1)
        lo = max(1, min(lo, hi - least + 1))
        return lo, hi, True
    return s, e, False


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


_PAGINATION_NOTES = {
    "approx": ("page numbers are APPROXIMATE (char-estimated — this file has no "
               "reliable page breaks, so they may not match a Word viewer's "
               "printed pages)"),
    "word": ("total page count is Word's own; the text mapped to each page is "
             "an approximate slice, so a specific page's content may be "
             "slightly off"),
}


def _summarize_paged(fp, name, pages, role):
    """Paginate the doc, validate the requested page range, run the map-reduce
    summariser, and assemble the result dict (or a structured error)."""
    from aiforge_core.runtime import doc_extract, doc_summarize
    paged, kind = doc_extract.paginate(fp, "")
    total = len(paged)
    note = _PAGINATION_NOTES.get(kind)
    # Out-of-range page request → don't fail silently; tell the model the real
    # page count so it can relay that instead of retrying blindly.
    if pages and not doc_extract.parse_page_spec(pages, total):
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


def _t_summarize_doc(args: dict, cwd: str) -> dict:
    """Summarise an attached/loaded document (pdf / docx / xlsx). Optional
    ``pages`` (e.g. "10-20", "3,5,7-9") summarises ONLY those pages/sections via
    the map-reduce summariser, so a 400-page report can be read section by
    section. No ``pages`` → the whole document."""
    import os as _os
    path = str(args.get("path") or args.get("file")
               or args.get("filename") or "").strip()
    if not path:
        return {"ok": False, "error": "need a file path/name"}
    fp = _resolve_doc(path, cwd)
    if not fp:
        return {"ok": False, "error": f"file not found: {path}"}
    pages = str(args.get("pages") or "").strip() or None
    role = str(args.get("role") or "chat").strip() or "chat"
    name = _os.path.basename(fp)
    return _summarize_paged(fp, name, pages, role)


_RENAME_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".go", ".rs",
                ".c", ".cpp", ".h", ".cs", ".rb", ".php", ".kt", ".scala",
                ".swift")
_RENAME_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "dist", "build",
                     "__pycache__"}


def _source_files(root: str):
    """Every source file under ``root``, vendor/build dirs pruned."""
    import os as _os
    for dp, dn, fns in _os.walk(root):
        dn[:] = [d for d in dn if d not in _RENAME_SKIP_DIRS]
        for fn in fns:
            if fn.endswith(_RENAME_EXTS):
                yield _os.path.join(dp, fn)


def _rename_in_file(fpath: str, pat, new: str, dry: bool) -> int:
    """Occurrences in this file; rewrites it unless ``dry``. 0 when unreadable
    or unmatched."""
    try:
        with open(fpath, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except Exception:  # noqa: BLE001
        return 0
    count = len(pat.findall(txt))
    if not count or dry:
        return count
    try:
        with open(fpath, "w", encoding="utf-8") as fh:
            fh.write(pat.sub(new, txt))
    except Exception:  # noqa: BLE001
        return 0
    return count


def _t_rename_symbol(args: dict, cwd: str) -> dict:
    import os as _os
    import re as _re
    name = str(args.get("name") or "")
    new = str(args.get("new_name") or "")
    if not name or not new:
        return {"ok": False, "error": "need 'name' and 'new_name'"}
    dry = bool(args.get("dry_run", True))
    base = str(args.get("path") or ".")
    root_p = base if _os.path.isabs(base) else _os.path.join(cwd or ".", base)
    pat = _re.compile(r"\b" + _re.escape(name) + r"\b")
    hits, changed = [], 0
    for fpath in _source_files(root_p):
        count = _rename_in_file(fpath, pat, new, dry)
        if not count:
            continue
        hits.append({"file": _os.path.relpath(fpath, cwd or "."),
                     "occurrences": count})
        if not dry:
            changed += count
    return {"ok": True, "name": name, "new_name": new, "dry_run": dry,
            "files": hits,
            "total_occurrences": sum(h["occurrences"] for h in hits),
            "applied": (0 if dry else changed)}
