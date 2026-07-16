"""Optional tree-sitter Java grammar wiring + ingest tunables (split from the
original ``treesitter_ingest`` module — verbatim move, no behaviour change)."""
from __future__ import annotations

# tree-sitter + the Java grammar are declared deps (pyproject), but guard the
# import so a broken/absent wheel degrades the symbol index instead of crashing
# module import (and everything that transitively imports it) at startup.
try:
    import tree_sitter_java as tsjava
    from tree_sitter import Language, Parser
    JAVA_LANG = Language(tsjava.language())
    JAVA_PARSER = Parser(JAVA_LANG)
    TREESITTER_AVAILABLE = True
except Exception:  # pragma: no cover — only when the wheel is missing/broken
    tsjava = None  # type: ignore
    Language = Parser = None  # type: ignore
    JAVA_LANG = JAVA_PARSER = None  # type: ignore
    TREESITTER_AVAILABLE = False


# ─────────────── tunables ───────────────

#: Skip method-body symbol extraction (locals, anonymous classes) when a
#: file is bigger than this. Class/method declarations themselves still get
#: ingested. Matches the spec's "handle large files" requirement.
LARGE_FILE_LINE_THRESHOLD = 10_000

#: Files larger than this in bytes are skipped outright (likely generated
#: or vendored). 4 MiB of Java is essentially never hand-written.
HARD_FILE_BYTE_LIMIT = 4 * 1024 * 1024

from aiforge_core.indexing.noise import EXCLUDE_DIRS as DEFAULT_EXCLUDE_DIRS  # shared filter

LOG_EVERY_N_FILES = 50
