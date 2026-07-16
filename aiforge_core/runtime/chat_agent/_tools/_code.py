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
