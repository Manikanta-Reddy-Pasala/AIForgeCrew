"""Data records emitted by the tree-sitter ingest (split from the original
``treesitter_ingest`` module — verbatim move, no behaviour change)."""
from __future__ import annotations

from dataclasses import dataclass, field


# ─────────────── data classes ───────────────

@dataclass
class FileRecord:
    path: str
    repo: str
    sha1: str
    language: str
    package: str
    loc: int


@dataclass
class SymbolRecord:
    fqn: str
    simple: str
    kind: str  # class | interface | enum | record | method | field
    file_path: str
    repo: str
    start_line: int
    end_line: int
    return_type: str = ""
    param_types: list[str] = field(default_factory=list)
    modifiers: list[str] = field(default_factory=list)


@dataclass
class FileParseResult:
    file: FileRecord
    symbols: list[SymbolRecord] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    extends_edges: list[tuple[str, str]] = field(default_factory=list)
    implements_edges: list[tuple[str, str]] = field(default_factory=list)
    # caller_fqn -> list of callee simple-names. We resolve simple-name to
    # fqn at ingest time using the global symbol table.
    call_simples: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class IngestStats:
    files_seen: int = 0
    files_parsed: int = 0
    files_skipped_unchanged: int = 0
    files_skipped_too_big: int = 0
    files_failed: int = 0
    symbols_written: int = 0
    calls_written: int = 0
    imports_written: int = 0
    extends_written: int = 0
    implements_written: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "files_seen": self.files_seen,
            "files_parsed": self.files_parsed,
            "files_skipped_unchanged": self.files_skipped_unchanged,
            "files_skipped_too_big": self.files_skipped_too_big,
            "files_failed": self.files_failed,
            "symbols_written": self.symbols_written,
            "calls_written": self.calls_written,
            "imports_written": self.imports_written,
            "extends_written": self.extends_written,
            "implements_written": self.implements_written,
            "duration_s": round(self.finished_at - self.started_at, 2),
        }
