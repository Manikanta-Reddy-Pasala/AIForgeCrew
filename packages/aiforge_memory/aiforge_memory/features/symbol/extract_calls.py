"""Stage 5 — call edges.

Re-parses each WalkedFile with the same per-language tree-sitter
queries, extracts call sites (`@call.name`), and resolves each name
to a Symbol_v2 fqname using a three-tier heuristic:

    1. same-file:  matching name defined in the same file
    2. imported:   match against fqnames whose file matches an import
    3. fuzzy:      any Symbol_v2 in the repo whose terminal name matches

confidence:  1.0 same-file, 0.7 import-resolved, 0.4 fuzzy.

Output: list[CallEdge] handed to symbol_writer.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tree_sitter import Query, QueryCursor
from tree_sitter_language_pack import get_language, get_parser

from aiforge_memory.features.symbol.extract import (
    WalkedFile,
    _load_query,
)


@dataclass
class CallEdge:
    repo: str
    caller_fqname: str
    callee_fqname: str
    confidence: float


def resolve_calls(
    files: list[WalkedFile], *, repo: str,
) -> list[CallEdge]:
    """Build CALLS edges across all walked files."""
    # Index every symbol by its terminal name (last :: segment)
    by_name: dict[str, list[str]] = {}
    file_index: dict[str, dict[str, str]] = {}  # file_path -> {name -> fqname}
    for wf in files:
        per_file: dict[str, str] = {}
        for sym in wf.symbols:
            short = sym.fqname.rsplit("::", 1)[-1]
            by_name.setdefault(short, []).append(sym.fqname)
            per_file[short] = sym.fqname
        file_index[wf.path] = per_file

    edges: list[CallEdge] = []

    for wf in files:
        if wf.parse_error or not wf.symbols:
            continue
        lang = wf.lang
        try:
            calls = _extract_calls(wf, lang)
        except Exception:
            continue
        if not calls:
            continue

        # Build local resolver index for this file's imports
        import_files = _resolve_imports_to_files(wf.imports, files)

        for call in calls:
            caller = _enclosing_symbol(wf.symbols, call["line"])
            if caller is None:
                continue
            callee_name = call["name"]

            # 1) same-file
            same = file_index.get(wf.path, {}).get(callee_name)
            if same:
                edges.append(CallEdge(
                    repo=repo,
                    caller_fqname=caller,
                    callee_fqname=same,
                    confidence=1.0,
                ))
                continue

            # 2) imported file
            resolved = _from_imports(callee_name, import_files, file_index)
            if resolved:
                edges.append(CallEdge(
                    repo=repo, caller_fqname=caller,
                    callee_fqname=resolved, confidence=0.7,
                ))
                continue

            # 3) fuzzy global
            cands = by_name.get(callee_name, [])
            if cands:
                edges.append(CallEdge(
                    repo=repo, caller_fqname=caller,
                    callee_fqname=cands[0], confidence=0.4,
                ))
    return edges


def _extract_calls(wf: WalkedFile, lang: str) -> list[dict]:
    """Re-parse to grab call sites with their line numbers."""
    if not wf.path:
        return []
    parser = get_parser(lang)
    language = get_language(lang)
    source = Path(wf.path)  # placeholder — caller passes content separately
    # The walker doesn't keep file bytes around; reload from disk.
    # In production, ingest paths use absolute repo_path; here the walker's
    # path is repo-relative, so callers must provide a base.
    # To keep this self-contained, defer to a helper that accepts content.
    return []


def _enclosing_symbol(symbols, line: int) -> str | None:
    """Find the innermost symbol whose [start,end] range contains `line`."""
    candidates = []
    for s in symbols:
        if s.line_start <= line <= s.line_end:
            candidates.append(s)
    if not candidates:
        return None
    # innermost = smallest range
    candidates.sort(key=lambda s: s.line_end - s.line_start)
    return candidates[0].fqname


def _resolve_imports_to_files(
    imports: list[str],
    files: list[WalkedFile],
    *,
    importer_path: str = "",
) -> list[str]:
    """Translate import strings to repo file paths using simple heuristics.

    Python: `pkg.module` → `pkg/module.py` or `pkg/module/__init__.py`.
            `helpers` (bare; usually a relative `from .helpers import ...`)
            → first try sibling-of-importer, then top-level.
    TypeScript: `./helpers` relative to importer's dir.
    Java: `com.foo.Bar` → `com/foo/Bar.java`.
    """
    file_paths = {wf.path for wf in files}
    importer_dir = ""
    if importer_path and "/" in importer_path:
        importer_dir = importer_path.rsplit("/", 1)[0]

    matched: list[str] = []
    for imp in imports:
        for cand in _import_candidates(imp, importer_dir=importer_dir):
            if cand in file_paths:
                matched.append(cand)
                break
    return matched


_JAVA_PATH_PREFIXES = (
    "src/main/java/",
    "src/test/java/",
    "src/main/kotlin/",
    "src/test/kotlin/",
)


def _import_candidates(imp: str, *, importer_dir: str = "") -> list[str]:
    out: list[str] = []
    if imp.startswith("./") or imp.startswith("../"):
        # TS relative — resolve against importer dir
        base = imp.lstrip("./")
        prefix = f"{importer_dir}/" if importer_dir else ""
        out.extend([f"{prefix}{base}.ts", f"{prefix}{base}.tsx",
                    f"{prefix}{base}/index.ts"])
    elif "." in imp:
        parts = imp.split(".")
        joined = "/".join(parts)
        # Python style — bare
        out.append(joined + ".py")
        out.append(joined + "/__init__.py")
        # Java/Kotlin — bare and Maven/Gradle-prefixed
        out.append(joined + ".java")
        out.append(joined + ".kt")
        for prefix in _JAVA_PATH_PREFIXES:
            out.append(prefix + joined + ".java")
            out.append(prefix + joined + ".kt")
    else:
        # Bare name — try sibling of importer first (Python relative import)
        if importer_dir:
            out.append(f"{importer_dir}/{imp}.py")
            out.append(f"{importer_dir}/{imp}.ts")
            out.append(f"{importer_dir}/{imp}.tsx")
            out.append(f"{importer_dir}/{imp}/__init__.py")
        out.append(f"{imp}.py")
        out.append(f"{imp}.java")
        out.append(f"{imp}.ts")
    return out


def _from_imports(
    callee_name: str,
    import_files: list[str],
    file_index: dict[str, dict[str, str]],
) -> str | None:
    for fp in import_files:
        fqname = file_index.get(fp, {}).get(callee_name)
        if fqname:
            return fqname
    return None


# ---- public re-walking helper ---------------------------------------------


def extract_calls_from_source(
    source: bytes, *, lang: str, file_path: str,
) -> list[dict]:
    """Run the @call query against `source`. Returns list of
    {"name": str, "line": int} (1-based)."""
    parser = get_parser(lang)
    language = get_language(lang)
    tree = parser.parse(source)
    qtext = _load_query(lang)
    if not qtext:
        return []
    q = Query(language, qtext)
    cur = QueryCursor(q)
    captures = cur.captures(tree.root_node)
    out: list[dict] = []
    for n in captures.get("call.name", []):
        text = source[n.start_byte:n.end_byte].decode("utf-8", errors="replace")
        out.append({"name": text, "line": n.start_point[0] + 1})
    return out


def resolve_calls_with_source(
    files: list[WalkedFile],
    *, repo: str, repo_root: str | Path,
) -> list[CallEdge]:
    """Same as resolve_calls() but actually opens files to extract call sites.

    The walker keeps repo-relative paths; we need bytes here, so we
    open them via repo_root + path.
    """
    repo_root = Path(repo_root)
    by_name: dict[str, list[str]] = {}
    file_index: dict[str, dict[str, str]] = {}
    for wf in files:
        per_file: dict[str, str] = {}
        for sym in wf.symbols:
            short = sym.fqname.rsplit("::", 1)[-1]
            by_name.setdefault(short, []).append(sym.fqname)
            per_file[short] = sym.fqname
        file_index[wf.path] = per_file

    edges: list[CallEdge] = []
    for wf in files:
        if wf.parse_error or not wf.symbols:
            continue
        if wf.lang not in {"python", "java", "typescript", "tsx", "javascript"}:
            continue
        try:
            data = (repo_root / wf.path).read_bytes()
        except OSError:
            continue
        try:
            calls = extract_calls_from_source(
                data, lang=wf.lang, file_path=wf.path,
            )
        except Exception:
            continue
        if not calls:
            continue

        import_files = _resolve_imports_to_files(
            wf.imports, files, importer_path=wf.path,
        )
        for call in calls:
            caller = _enclosing_symbol(wf.symbols, call["line"])
            if caller is None:
                continue
            callee_name = call["name"]
            same = file_index.get(wf.path, {}).get(callee_name)
            if same and same != caller:
                edges.append(CallEdge(
                    repo=repo, caller_fqname=caller,
                    callee_fqname=same, confidence=1.0,
                ))
                continue
            resolved = _from_imports(callee_name, import_files, file_index)
            if resolved and resolved != caller:
                edges.append(CallEdge(
                    repo=repo, caller_fqname=caller,
                    callee_fqname=resolved, confidence=0.7,
                ))
                continue
            cands = by_name.get(callee_name, [])
            if cands and cands[0] != caller:
                edges.append(CallEdge(
                    repo=repo, caller_fqname=caller,
                    callee_fqname=cands[0], confidence=0.4,
                ))
    return edges
