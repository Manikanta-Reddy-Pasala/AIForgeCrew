"""Language-agnostic tag-query helpers used by the generic (non-Java) extractor
(split from the original ``treesitter_ingest`` module — verbatim move, no
behaviour change). The monkeypatch-coupled aider glue (``_import_aider`` /
``_get_repomap``) and ``_parse_via_tags`` itself stay in the package
``__init__`` so tests that patch ``tsi.*`` observe them."""
from __future__ import annotations

import re
from pathlib import Path

# Node-type substrings that mark a definition's enclosing declaration. Matched
# against tree-sitter node ``type`` names, which vary per grammar but are
# stable within these families (e.g. ``class_declaration``, ``class_specifier``,
# ``struct_specifier``, ``function_definition``, ``function_declarator``,
# ``method_declaration``, ``object_declaration``).
_CLASS_NODE_HINTS = (
    "class", "struct", "interface", "enum", "trait", "object_declaration",
    "record", "namespace", "type_alias", "module",
)
_FUNC_NODE_HINTS = ("function", "method", "constructor", "lambda", "subroutine")


def _tag_parser(lang: str):
    """tree-sitter parser for ``lang`` via tree-sitter-language-pack (the same
    grammar set aider uses). Returns None on any failure so classification
    falls back to the name-shape heuristic."""
    try:
        from tree_sitter_language_pack import get_parser
        return get_parser(lang)
    except Exception:  # noqa: BLE001
        return None


def _classify_def(root_node, name: str, line: int) -> str:
    """Classify a definition as ``class`` or ``method`` by walking up from the
    name node to its enclosing declaration. Callables are uniformly ``method``
    (not split function/method) so the existing method-only :CALLS writer can
    resolve cross-language calls. Falls back to name shape when the node can't
    be located (TitleCase → class, else method)."""
    if root_node is not None:
        want = name.encode("utf-8", errors="replace")
        # Collect candidate name nodes matching (text, line).
        cands = []
        stack = [root_node]
        while stack:
            n = stack.pop()
            if n.is_named and n.start_point[0] == line and n.text == want:
                cands.append(n)
            stack.extend(n.children)
        for nn in cands:
            p = nn.parent
            depth = 0
            while p is not None and depth < 15:
                t = p.type
                if any(h in t for h in _CLASS_NODE_HINTS):
                    return "class"
                if any(h in t for h in _FUNC_NODE_HINTS):
                    return "method"
                p = p.parent
                depth += 1
    return "class" if name[:1].isupper() else "method"


# Lightweight import scan (secondary signal — imports are only wired as
# :IMPORTS edges when they match a :Symbol fqn, which is rare for external
# libs, so this stays deliberately simple).
_PY_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))",
                           re.MULTILINE)
_JS_IMPORT_RE = re.compile(r"""import\s+.*?from\s+['"]([^'"]+)['"]""")


def _scan_imports(text: str, lang: str) -> list[str]:
    out: list[str] = []
    try:
        if lang == "python":
            for m in _PY_IMPORT_RE.finditer(text):
                mod = m.group(1) or m.group(2)
                if mod:
                    out.append(mod.strip())
        elif lang in ("javascript", "typescript", "tsx", "jsx"):
            out.extend(m.group(1).strip() for m in _JS_IMPORT_RE.finditer(text))
    except Exception:  # noqa: BLE001
        return []
    # de-dup, preserve order
    seen: set[str] = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def _module_for(fpath: Path, repo_root: "Path | None") -> str:
    """Derive the module/package qualifier used in ``fqn`` and ``FileRecord.
    package``. With ``repo_root`` the file's repo-relative path (slashes → dots,
    extension dropped) — a python dotted module for .py, and a unique per-file
    qualifier for others (avoids fqn collisions between same-stem files). Falls
    back to the bare file stem when no root is available (unit tests)."""
    if repo_root is not None:
        try:
            rel = fpath.resolve().relative_to(Path(repo_root).resolve())
        except Exception:  # noqa: BLE001 — not under root
            rel = Path(fpath.name)
        parts = [p for p in rel.with_suffix("").parts if p not in ("", ".")]
        if parts:
            return ".".join(parts)
    return fpath.stem
