"""LSP-driven CALLS resolution.

Given a list of WalkedFile (from tree-sitter walk), produces high-
confidence CallEdge[] by asking the language server for *references* to
each method/function symbol. A reference inside another symbol's
line-range becomes a (caller -> callee) CALLS edge with confidence=1.0.

Run per-language: a single LSP server processes all files of its
language. Mixed-language repos spin up multiple servers in sequence
(not parallel — most LSPs are single-tenant per workspace).

Public surface:
    resolve_calls(walked_files, *, repo, repo_root, langs=None)
        -> list[CallEdge]

Behaviour when adapter unavailable:
    Returns empty list — caller falls back to tree-sitter heuristic
    (`edges.resolve_calls_with_source`).

Confidence policy:
    - LSP-confirmed references: 1.0
    - Tree-sitter heuristic (existing): 0.7 (import-aware) / 1.0 (same-file)
    The CALLS edge writer keeps the highest confidence when both fire.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiforge_memory.features.lsp.adapters import adapter_for
from aiforge_memory.features.lsp.client import LspClient, LspError, uri_to_path
from aiforge_memory.features.symbol.extract import WalkedFile, WalkedSymbol
from aiforge_memory.features.symbol.extract_calls import CallEdge

# Symbol kinds that can be call targets
_CALL_TARGET_KINDS = {"method", "function"}


@dataclass(frozen=True)
class _SymRange:
    fqname: str
    file_path: str           # repo-relative
    line_start: int          # 1-based
    line_end: int            # 1-based


def resolve_calls(
    walked_files: list[WalkedFile],
    *,
    repo: str,
    repo_root: str | Path,
    langs: list[str] | None = None,
) -> list[CallEdge]:
    """Run LSP per-language; aggregate CallEdges across all langs."""
    repo_root = Path(repo_root).resolve()

    by_lang: dict[str, list[WalkedFile]] = {}
    for wf in walked_files:
        if wf.parse_error:
            continue
        by_lang.setdefault(wf.lang, []).append(wf)

    target_langs = langs if langs is not None else list(by_lang)
    out: list[CallEdge] = []
    for lang in target_langs:
        files = by_lang.get(lang, [])
        if not files:
            continue
        adapter = adapter_for(lang)
        if adapter is None:
            continue
        cmd, language_id, init_opts = adapter
        try:
            edges = _resolve_one_lang(
                files, repo=repo, repo_root=repo_root,
                lang=lang, language_id=language_id,
                cmd=cmd, init_opts=init_opts,
            )
        except LspError:
            edges = []
        out.extend(edges)
    return out


def _resolve_one_lang(
    files: list[WalkedFile], *,
    repo: str,
    repo_root: Path,
    lang: str, language_id: str,
    cmd: list[str], init_opts: dict,
) -> list[CallEdge]:
    """Boot one LSP server for the language; ask for refs of each symbol."""
    sym_index = _build_sym_index(files)        # file_path -> sorted ranges
    all_targets = [
        (wf, sym)
        for wf in files
        for sym in wf.symbols
        if sym.kind in _CALL_TARGET_KINDS
    ]
    if not all_targets:
        return []

    root_uri = "file://" + str(repo_root)
    edges: list[CallEdge] = []
    seen: set[tuple[str, str]] = set()

    with LspClient(
        cmd, root_uri=root_uri, initialization_options=init_opts,
        server_name=lang,
    ) as cli:
        # Open every file once so the server has full project context.
        for wf in files:
            try:
                cli.did_open(repo_root / wf.path, language_id=language_id)
            except LspError:
                continue

        for wf, sym in all_targets:
            target_path = repo_root / wf.path
            line_zero, col_zero = _name_position(sym, source_path=target_path)
            try:
                refs = cli.references(
                    target_path,
                    line=line_zero, character=col_zero,
                    include_declaration=False,
                )
            except LspError:
                continue
            for ref in refs:
                edge = _ref_to_edge(
                    ref=ref, callee=sym, sym_index=sym_index,
                    repo_root=repo_root, repo=repo,
                )
                if edge is None:
                    continue
                key = (edge.caller_fqname, edge.callee_fqname)
                if key in seen:
                    continue
                seen.add(key)
                edges.append(edge)
    return edges


# ─── support ──────────────────────────────────────────────────────────

def _build_sym_index(files: list[WalkedFile]) -> dict[str, list[_SymRange]]:
    """Per-file sorted symbol ranges for fast enclosing-symbol lookup."""
    out: dict[str, list[_SymRange]] = {}
    for wf in files:
        ranges = [
            _SymRange(
                fqname=s.fqname, file_path=wf.path,
                line_start=s.line_start, line_end=s.line_end,
            )
            for s in wf.symbols
            if s.kind in _CALL_TARGET_KINDS or s.kind == "class"
        ]
        # Smallest enclosing wins → sort by span ascending
        ranges.sort(key=lambda r: (r.line_end - r.line_start, r.line_start))
        out[wf.path] = ranges
    return out


def _name_position(
    sym: WalkedSymbol, *, source_path: Path,
) -> tuple[int, int]:
    """Locate the symbol name on its first source line. LSP positions
    are zero-based; tree-sitter line_start is 1-based."""
    line0 = max(0, sym.line_start - 1)
    name = sym.fqname.rsplit("::", 1)[-1]
    try:
        with open(source_path, "rb") as f:
            data = f.read()
    except OSError:
        return line0, 0
    lines = data.split(b"\n")
    if line0 >= len(lines):
        return line0, 0
    raw = lines[line0].decode("utf-8", errors="replace")
    col = raw.find(name)
    if col < 0:
        col = 0
    return line0, col


def _ref_to_edge(
    *, ref: dict, callee: WalkedSymbol,
    sym_index: dict[str, list[_SymRange]], repo_root: Path, repo: str,
) -> CallEdge | None:
    """Find the smallest enclosing symbol for a reference; emit a
    CALLS edge from it to the callee. Skip self-references."""
    uri = ref.get("uri") or ""
    rng = ref.get("range") or {}
    start = rng.get("start") or {}
    line_zero = start.get("line")
    if line_zero is None:
        return None
    abs_path = uri_to_path(uri)
    try:
        rel_path = str(Path(abs_path).resolve().relative_to(repo_root))
    except ValueError:
        return None
    ranges = sym_index.get(rel_path) or []
    enclosing = _enclosing(ranges, line_one=line_zero + 1)
    if enclosing is None:
        return None
    if enclosing.fqname == callee.fqname:
        return None        # self-ref / declaration
    return CallEdge(
        repo=repo,
        caller_fqname=enclosing.fqname,
        callee_fqname=callee.fqname,
        confidence=1.0,
    )


def _enclosing(ranges: list[_SymRange], *, line_one: int) -> _SymRange | None:
    """First (smallest-span) range that contains the 1-based line."""
    for r in ranges:
        if r.line_start <= line_one <= r.line_end:
            return r
    return None
