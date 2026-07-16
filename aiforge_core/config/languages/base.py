"""Frozen per-language knowledge record.

A :class:`LanguageProfile` is the single, portable description of what dev
activity looks like for one language: how to compile / test / lint / format
it, which files mark a project of that language, how to sniff its syntax
without executing it, which host binaries realise its toolchain, and a short
doer-facing note of idioms + gotchas.

The three legacy consumers (``config.repo_standards``,
``runtime.integration_report``, ``runtime.syntax_guard``) each held a slice of
this knowledge as hardcoded literals. Profiles consolidate that knowledge; the
consumers are wired to mirror/source it (and tests pin registry == legacy for
the languages that were already supported, so no output shifts).

Design notes:
- ``compile_cmd`` / ``test_cmd`` / ``lint_cmd`` / ``format_cmd`` are the exact
  command strings the older ``_DEFAULTS_BY_LANG`` used — copied verbatim.
- ``syntax_check`` is ``(binary, argv_builder)`` for a NON-EXECUTING parse
  check, mirroring ``syntax_guard._CHECKERS``. ``argv_builder(binary, path)``
  returns the argv list. ``None`` when the language has no cheap external
  checker (Python → in-process ``compile()``; Rust / Kotlin → heuristic).
- ``toolchain_candidates`` maps a logical tool to the ordered binary names to
  probe on ``PATH`` (first hit wins), e.g. ``{"python": ("python3", "python")}``.
- ``conventions`` is a short doer-facing note (idioms, test framework, common
  gotchas). Kept intentionally terse so it is cheap to inject into a prompt.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass(frozen=True)
class LanguageProfile:
    """Immutable description of one programming language's dev activity."""

    name: str
    aliases: tuple[str, ...] = ()
    extensions: tuple[str, ...] = ()
    # Marker files that identify a project of this language (pom.xml,
    # Cargo.toml, CMakeLists.txt, pyproject.toml, ...).
    build_markers: tuple[str, ...] = ()
    # Dev-activity commands. Copied verbatim from the legacy literals so the
    # rewired consumers emit byte-identical output for already-supported langs.
    compile_cmd: str = ""
    test_cmd: str = ""
    lint_cmd: str = ""
    format_cmd: str = ""
    # Non-executing syntax check: (binary, lambda binary, filepath -> argv) or
    # None when the language falls back to Python compile() / the brace
    # heuristic.
    syntax_check: Optional[tuple[str, Callable[[str, str], list]]] = None
    # Logical tool -> ordered binary candidates to probe on PATH.
    toolchain_candidates: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # Human-facing "install this" hint when the toolchain is absent.
    install_hint: str = ""
    # Short doer-facing idioms / test framework / gotchas note.
    conventions: str = ""


__all__ = ["LanguageProfile"]
